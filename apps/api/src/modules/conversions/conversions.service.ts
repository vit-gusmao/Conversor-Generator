import { forwardRef, Inject, Injectable, Logger } from '@nestjs/common';
import type { Conversion, Prisma } from '@prisma/client';
import {
  ConversionOptionsError,
  canonicalOptionsJson,
  normalizeConversionOptions,
  type ConversionListItem,
  type ConversionOptionsV2,
  type ConversionOptionsV3,
  type Paginated,
  type Preview,
  type ResultOverview,
  type ResultSummary,
  type SheetPage,
  type SheetSourcePages,
} from '@pdf2x/contracts';

import { ApiError } from '../../common/errors/api-error';
import { requestFingerprint } from '../../common/fingerprint';
import { ERROR_CODES } from '../../common/errors/error-codes';
import type { AuthenticatedUser } from '../../common/request-context';
import {
  asConversionStatus,
  isTerminal,
  progressFloorForStatus,
  stageKeyForStatus,
} from '../../common/status';
import { MAX_PAGE_SIZE, type ListConversionsQueryDto } from '../../common/dto/pagination.dto';
import { AppConfigService } from '../../config/app-config.service';
import { PrismaService } from '../../database/prisma.service';
import { FileValidationService } from '../../upload/file-validation.service';
import { STORAGE_SERVICE, storageKeyForSha256, type StorageService } from '../../upload/storage.service';
import {
  extensionForImageFormat,
  type FileInspection,
} from '../../upload/file-validation.service';
import { BatchesService } from '../batches/batches.service';
import { ConversionEventsService } from '../realtime/conversion-events.service';

import { ConverterClient, type DownloadPayload } from './converter.client';
import type {
  CancelConversionResponse,
  CreateConversionResponse,
  UploadedPdfFile,
} from './dto/conversion-response.dto';
import type { PreviewQueryDto } from './dto/preview-query.dto';
import type { PageListResponse, TableListResponse } from './converter.client';
import {
  linksFor,
  statusOf,
  toConversionDetail,
  toConversionListItem,
  toValidationReport,
  type ConversionWithRelations,
} from './conversions.mapper';
import { OUTBOX_ENQUEUE, OutboxService } from './outbox.service';
import { assertNotTerminal, assertOwnership } from './ownership';

/** Rows written by one queued-conversion transaction. */
interface QueuedInsert {
  conversion: Conversion;
  runId: string;
  outboxId: string;
}

/** Prisma's unique-constraint violation (P2002), e.g. a racing duplicate. */
function isUniqueViolation(error: unknown): boolean {
  return typeof error === 'object' && error !== null && (error as { code?: unknown }).code === 'P2002';
}

/** A document already persisted in object storage. */
export interface StoredSource {
  storageKey: string;
  sha256: string;
  sizeBytes: number;
  filename: string;
  mimeType: string;
  pageCount?: number | null;
  validation?: import('@pdf2x/contracts').ValidationReport | null;
}

@Injectable()
export class ConversionsService {
  private readonly logger = new Logger(ConversionsService.name);

  constructor(
    private readonly prisma: PrismaService,
    private readonly validation: FileValidationService,
    @Inject(STORAGE_SERVICE) private readonly storage: StorageService,
    private readonly converter: ConverterClient,
    private readonly config: AppConfigService,
    private readonly events: ConversionEventsService,
    private readonly outbox: OutboxService,
    @Inject(forwardRef(() => BatchesService)) private readonly batches: BatchesService,
  ) {}

  /* ------------------------------- create -------------------------------- */

  async create(
    user: AuthenticatedUser,
    file: UploadedPdfFile | undefined,
    idempotencyKey?: string,
    rawOptions?: unknown,
  ): Promise<CreateConversionResponse> {
    if (!file || !file.buffer || file.buffer.length === 0) {
      throw ApiError.badRequest(ERROR_CODES.INVALID_FILE, { reason: 'empty_file' });
    }

    const filename = this.safeFilename(file.originalname);
    const inspection = this.validation.validate(file.buffer, {
      filename,
      mimeType: file.mimetype,
    });

    const isImage = inspection.kind === 'image' && Boolean(inspection.image_format);
    const extension = isImage
      ? extensionForImageFormat(inspection.image_format as string)
      : 'pdf';
    const mimeType = inspection.mime_type ?? (isImage ? 'image/*' : 'application/pdf');

    // Options are normalised once, here, and travel to the engine untouched.
    // Invalid choices (including any attempt to cap pages) fail before storage.
    const options = this.normalizeOptions(rawOptions, isImage ? 'image' : 'pdf');

    const sha256 = inspection.sha256;
    const storageKey = storageKeyForSha256(sha256, extension, user.id);

    await this.storage.put(storageKey, file.buffer, mimeType);

    return this.createStoredConversion(
      user,
      {
        storageKey,
        sha256,
        sizeBytes: file.buffer.length,
        filename,
        mimeType,
        pageCount: inspection.page_count > 0 ? inspection.page_count : null,
        validation: toValidationReport(inspection),
      },
      options,
      { idempotencyKey: idempotencyKey?.trim() },
    );
  }

  /**
   * Creates the conversion (document + run + outbox) for an object that is
   * already in storage.
   *
   * The resumable-upload path and the legacy multipart endpoint both land here,
   * so both produce identical rows, events and queue messages.
   */
  async createStoredConversion(
    user: AuthenticatedUser,
    source: StoredSource,
    options: ConversionOptionsV2 | ConversionOptionsV3,
    meta: { idempotencyKey?: string; documentId?: string } = {},
  ): Promise<CreateConversionResponse> {
    const { sha256, filename } = source;
    const key = meta.idempotencyKey?.trim();
    const fingerprint = requestFingerprint(sha256, options);

    if (key) {
      const existing = await this.prisma.idempotencyKey.findUnique({
        where: { ownerId_key: { ownerId: user.id, key } },
      });
      if (existing) {
        const replayed = await this.replayIdempotent(existing, fingerprint, sha256);
        if (replayed) return replayed;
      }
    }

    const queuedProgress = progressFloorForStatus('queued');
    let created: QueuedInsert;
    try {
      created = await this.insertQueuedConversion(user, source, options, {
        key,
        fingerprint,
        queuedProgress,
      });
    } catch (error) {
      // Two requests with the same key raced past the lookup above. The unique
      // index on (ownerId, key) rejected the later one, so the winner's
      // conversion is the answer for both (A-21).
      if (key && isUniqueViolation(error)) {
        const winner = await this.prisma.idempotencyKey.findUnique({
          where: { ownerId_key: { ownerId: user.id, key } },
        });
        if (winner) {
          const replayed = await this.replayIdempotent(winner, fingerprint, sha256);
          if (replayed) return replayed;
        }
      }
      throw error;
    }

    await this.events.record(created.conversion.id, {
      type: 'status',
      status: 'queued',
      stage: stageKeyForStatus('queued'),
      progress: queuedProgress,
      message: 'Conversão recebida e enfileirada.',
      runId: created.runId,
      payload: {
        filename,
        page_count: source.pageCount ?? null,
        target_formats: options.target_formats,
      },
    });

    // Fire and forget: the outbox row is durable, the drainer retries it.
    // dispatch() already persists failures, but it can still reject if the
    // failure write itself fails. Guard the process against an unhandled
    // rejection: the durable outbox row remains for the drainer.
    void this.outbox.dispatch(created.outboxId).catch((error: unknown) => {
      this.logger.warn(`Outbox ${created.outboxId} não pôde ser despachado: ${String(error)}`);
    });

    return this.queuedResponse(created.conversion, 'Conversão recebida e enfileirada.');
  }

  /**
   * Answers a request whose idempotency key already exists: the same payload
   * replays the stored conversion, a different payload is a conflict.
   * Returns null when the key points at a conversion that no longer exists.
   */
  private async replayIdempotent(
    existing: { requestHash: string; conversionId: string },
    fingerprint: string,
    sha256: string,
  ): Promise<CreateConversionResponse | null> {
    const matches = existing.requestHash === fingerprint || existing.requestHash === sha256;
    if (!matches) {
      throw ApiError.conflict(ERROR_CODES.IDEMPOTENT_REPLAY, { reason: 'payload_mismatch' });
    }
    const replayed = await this.findRow(existing.conversionId);
    return replayed ? this.queuedResponse(replayed, 'Conversão já recebida anteriormente.') : null;
  }

  /** Writes document, conversion, first run and outbox row atomically. */
  private async insertQueuedConversion(
    user: AuthenticatedUser,
    source: StoredSource,
    options: ConversionOptionsV2 | ConversionOptionsV3,
    meta: { key?: string; fingerprint: string; queuedProgress: number },
  ): Promise<QueuedInsert> {
    const { storageKey, sha256, sizeBytes, filename, mimeType } = source;
    const { key, fingerprint, queuedProgress } = meta;

    return this.prisma.$transaction(async (tx) => {
      const document = await tx.document.create({
        data: {
          filename,
          mimeType,
          sizeBytes,
          sha256,
          storageKey,
          pageCount: source.pageCount ?? null,
          ownerId: user.id,
        },
      });

      const conversion = await tx.conversion.create({
        data: {
          documentId: document.id,
          ownerId: user.id,
          status: 'received',
          stage: 'receive',
          progress: 0,
          quality: 'pending',
          validationJson: source.validation ? JSON.stringify(source.validation) : null,
          optionsJson: canonicalOptionsJson(options),
        },
      });

      const run = await tx.conversionRun.create({
        data: {
          conversionId: conversion.id,
          attempt: 1,
          status: 'received',
          stage: 'receive',
          progress: 0,
          startedAt: new Date(),
        },
      });

      const queued = await tx.conversion.update({
        where: { id: conversion.id },
        data: {
          status: 'queued',
          stage: stageKeyForStatus('queued'),
          progress: queuedProgress,
          activeRunId: run.id,
        },
      });

      await tx.conversionRun.update({
        where: { id: run.id },
        data: { status: 'queued', stage: stageKeyForStatus('queued'), progress: queuedProgress },
      });

      if (key) {
        await tx.idempotencyKey.create({
          data: {
            key,
            ownerId: user.id,
            conversionId: conversion.id,
            requestHash: fingerprint,
          },
        });
      }

      // Outbox row is written in the same transaction as the conversion and
      // its run: a crash cannot leave a queued conversion without a durable
      // job message.
      const outbox = await this.outbox.record(
        OUTBOX_ENQUEUE,
        {
          conversionId: conversion.id,
          runId: run.id,
          storageKey,
          filename,
          options: { conversionOptions: options },
        },
        tx,
      );

      return { conversion: queued, runId: run.id, outboxId: outbox.id };
    });
  }

  /* --------------------------------- read -------------------------------- */

  async list(
    user: AuthenticatedUser,
    query: ListConversionsQueryDto,
  ): Promise<Paginated<ConversionListItem>> {
    const page = query.page && query.page > 0 ? query.page : 1;
    const pageSize = Math.min(Math.max(query.page_size ?? 20, 1), MAX_PAGE_SIZE);

    const where: Prisma.ConversionWhereInput = { ownerId: user.id, deletedAt: null };
    if (query.status) where.status = query.status;

    const search = query.q?.trim();
    if (search) {
      where.document = { filename: { contains: search } };
    }

    const [total, rows] = await Promise.all([
      this.prisma.conversion.count({ where }),
      this.prisma.conversion.findMany({
        where,
        include: { document: true },
        orderBy: { createdAt: 'desc' },
        skip: (page - 1) * pageSize,
        take: pageSize,
      }),
    ]);

    return {
      items: rows.map((row) => toConversionListItem(row)),
      total,
      page,
      page_size: pageSize,
    };
  }

  async getDetailResponse(user: AuthenticatedUser, id: string) {
    const row = await this.findOwned(id, user);
    return toConversionDetail(row);
  }

  async getResult(user: AuthenticatedUser, id: string): Promise<ResultSummary> {
    const row = await this.findOwned(id, user);
    const runId = this.requireRunId(row);
    return this.converter.result(runId);
  }

  /** Compact result payload used by the minimalist result screen. */
  async getResultOverview(user: AuthenticatedUser, id: string): Promise<ResultOverview> {
    const row = await this.findOwned(id, user);
    return this.converter.resultOverview(this.requireRunId(row));
  }

  async getSheets(
    user: AuthenticatedUser,
    id: string,
    query: { cursor?: string; limit?: number; q?: string },
  ): Promise<SheetPage> {
    const row = await this.findOwned(id, user);
    return this.converter.sheets(this.requireRunId(row), query);
  }

  async getSheetPages(
    user: AuthenticatedUser,
    id: string,
    sheetId: string,
    query: { cursor?: number; limit?: number },
  ): Promise<SheetSourcePages> {
    const row = await this.findOwned(id, user);
    return this.converter.sheetPages(this.requireRunId(row), sheetId, query);
  }

  async getPreview(
    user: AuthenticatedUser,
    id: string,
    query: PreviewQueryDto,
  ): Promise<Preview> {
    const row = await this.findOwned(id, user);
    const runId = this.requireRunId(row);
    return this.converter.preview(runId, {
      sheet: query.sheet,
      offset: query.offset,
      limit: query.limit,
    });
  }

  async getPages(user: AuthenticatedUser, id: string): Promise<PageListResponse> {
    const row = await this.findOwned(id, user);
    return this.converter.pages(this.requireRunId(row));
  }

  async getTables(user: AuthenticatedUser, id: string): Promise<TableListResponse> {
    const row = await this.findOwned(id, user);
    return this.converter.tables(this.requireRunId(row));
  }

  async getCellEvidence(
    user: AuthenticatedUser,
    id: string,
    cellId: string,
  ): Promise<Record<string, unknown>> {
    const row = await this.findOwned(id, user);
    return this.converter.cellEvidence(this.requireRunId(row), cellId);
  }

  async getDownload(user: AuthenticatedUser, id: string): Promise<DownloadPayload> {
    const row = await this.findOwned(id, user);
    const status = statusOf(row);
    if (!isTerminal(status) || status === 'failed' || status === 'cancelled') {
      throw ApiError.conflict(ERROR_CODES.NOT_READY, { status });
    }
    return this.converter.download(this.requireRunId(row));
  }

  /* -------------------------------- mutate ------------------------------- */

  async retry(user: AuthenticatedUser, id: string): Promise<CreateConversionResponse> {
    const row = await this.findOwned(id, user);
    const status = statusOf(row);

    if (!isTerminal(status)) {
      throw ApiError.conflict(ERROR_CODES.CONVERSION_BUSY, { status });
    }

    const progress = progressFloorForStatus('queued');
    const options = this.optionsForRow(row);

    // The attempt number is derived inside the transaction, so two retries
    // cannot both claim the same attempt. The unique (conversionId, attempt)
    // index is the final guard (A-22).
    let inserted: { run: { id: string; attempt: number }; updated: Conversion; outboxId: string };
    try {
      inserted = await this.prisma.$transaction(async (tx) => {
        const latest = await tx.conversionRun.findMany({
          where: { conversionId: row.id },
          orderBy: { attempt: 'desc' },
          take: 1,
          select: { attempt: true },
        });
        const attempt = (latest[0]?.attempt ?? 0) + 1;

        const run = await tx.conversionRun.create({
          data: {
            conversionId: row.id,
            attempt,
            status: 'queued',
            stage: stageKeyForStatus('queued'),
            progress,
            startedAt: new Date(),
          },
        });

        const updated = await tx.conversion.update({
          where: { id: row.id },
          data: {
            status: 'queued',
            stage: stageKeyForStatus('queued'),
            progress,
            quality: 'pending',
            activeRunId: run.id,
            errorCode: null,
            errorMessage: null,
          },
        });

        // Outbox row shares the transaction: no queued run without its job message.
        const outbox = await this.outbox.record(
          OUTBOX_ENQUEUE,
          {
            conversionId: row.id,
            runId: run.id,
            storageKey: row.document.storageKey,
            filename: row.document.filename,
            options: { conversionOptions: options },
          },
          tx,
        );

        return { run: { id: run.id, attempt }, updated, outboxId: outbox.id };
      });
    } catch (error) {
      if (isUniqueViolation(error)) {
        throw ApiError.conflict(ERROR_CODES.CONVERSION_BUSY, { status });
      }
      throw error;
    }

    const { run, updated, outboxId } = inserted;

    await this.events.record(row.id, {
      type: 'status',
      status: 'queued',
      stage: stageKeyForStatus('queued'),
      progress,
      message: `Nova tentativa iniciada (tentativa ${run.attempt}).`,
      runId: run.id,
      payload: { attempt: run.attempt },
    });

    // dispatch() already persists failures, but it can still reject if the
    // failure write itself fails. Guard the process against an unhandled
    // rejection: the durable outbox row remains for the drainer.
    void this.outbox.dispatch(outboxId).catch((error: unknown) => {
      this.logger.warn(`Outbox ${outboxId} não pôde ser despachado: ${String(error)}`);
    });

    return this.queuedResponse(updated, 'Nova tentativa iniciada.');
  }

  /**
   * Cancels the active run. The conversion is marked cancelled only when the
   * engine confirms it (A-17). When the engine refuses (the run already
   * published) or cannot be reached, its state is re-read and the local
   * conversion is left as it is.
   */
  async cancel(user: AuthenticatedUser, id: string): Promise<CancelConversionResponse> {
    const row = await this.findOwned(id, user);
    assertNotTerminal(row);

    if (row.activeRunId) {
      const confirmed = await this.confirmEngineCancel(row.activeRunId);
      if (!confirmed) {
        return {
          conversion_id: row.id,
          status: statusOf(row),
          message: 'O motor não confirmou o cancelamento; a conversão segue seu curso.',
          links: linksFor(row.id),
        };
      }
      await this.prisma.conversionRun.updateMany({
        where: { id: row.activeRunId },
        data: { status: 'cancelled', finishedAt: new Date() },
      });
    }

    const updated = await this.prisma.conversion.update({
      where: { id: row.id },
      data: { status: 'cancelled', stage: 'failed' },
    });

    await this.events.record(row.id, {
      type: 'failed',
      status: 'cancelled',
      stage: 'failed',
      progress: updated.progress,
      message: 'Conversão cancelada pelo usuário.',
      runId: row.activeRunId,
    });

    // No-op unless the conversion is linked to a batch item.
    try {
      await this.batches.onConversionTerminal(row.id);
    } catch (error) {
      this.logger.warn(`Falha ao atualizar o lote após cancelar ${row.id}: ${String(error)}`);
    }

    return {
      conversion_id: row.id,
      status: 'cancelled',
      message: 'Cancelamento solicitado.',
      links: linksFor(row.id),
    };
  }

  /**
   * True only when the engine says it cancelled the run. Any other outcome
   * (refused, unknown run, unreachable engine) is logged together with the
   * engine's current status, which is what the caller keeps as the truth.
   */
  private async confirmEngineCancel(runId: string): Promise<boolean> {
    try {
      const outcome = await this.converter.cancel(runId);
      if (outcome?.cancelled === true) return true;
    } catch (error) {
      this.logger.warn(`Falha ao solicitar cancelamento de ${runId}: ${String(error)}`);
    }

    try {
      const state = await this.converter.status(runId);
      this.logger.warn(`Cancelamento de ${runId} não confirmado; motor reporta ${state.status}`);
    } catch (error) {
      this.logger.warn(`Estado de ${runId} indisponível após cancelamento: ${String(error)}`);
    }
    return false;
  }

  async remove(user: AuthenticatedUser, id: string): Promise<void> {
    const row = await this.findOwned(id, user);
    const status = statusOf(row);

    await this.prisma.conversion.update({
      where: { id: row.id },
      data: { deletedAt: new Date() },
    });

    // Artifacts are purged best-effort; the PDF is only removed once nothing
    // can still be running (otherwise the engine loses its input mid-flight).
    if (isTerminal(status)) {
      // The object may be shared by several documents (same content). Only
      // remove it when no other document still references the key.
      try {
        const references = await this.prisma.document.count({
          where: { storageKey: row.document.storageKey, id: { not: row.document.id } },
        });
        if (references === 0) {
          await this.storage.delete(row.document.storageKey);
        }
      } catch (error) {
        this.logger.warn(`Falha ao remover o PDF ${row.document.storageKey}: ${String(error)}`);
      }
    } else if (row.activeRunId) {
      try {
        await this.converter.cancel(row.activeRunId);
      } catch {
        // ignore
      }
    }
  }

  /* ------------------------------- helpers ------------------------------- */

  async findRow(id: string): Promise<ConversionWithRelations | null> {
    return this.prisma.conversion.findUnique({
      where: { id },
      include: {
        document: true,
        runs: { orderBy: { attempt: 'desc' }, take: 1 },
      },
    });
  }

  private async findOwned(
    id: string,
    user: AuthenticatedUser,
  ): Promise<ConversionWithRelations> {
    const row = await this.findRow(id);
    if (!row) throw ApiError.notFound({ resource: 'conversion' });
    assertOwnership(row, user.id);
    return row;
  }

  private requireRunId(row: ConversionWithRelations): string {
    const runId = row.activeRunId ?? row.runs[0]?.id;
    if (!runId) throw ApiError.notFound({ resource: 'conversion_run' });
    return runId;
  }

  private queuedResponse(
    conversion: { id: string; status: string },
    message: string,
  ): CreateConversionResponse {
    return {
      conversion_id: conversion.id,
      status: asConversionStatus(conversion.status) ?? 'queued',
      message,
      links: linksFor(conversion.id),
    };
  }

  /**
   * Normalises user options, mapping contract errors onto the HTTP envelope.
   */
  normalizeOptions(
    rawOptions: unknown,
    kind: 'pdf' | 'image' | 'spreadsheet',
  ): ConversionOptionsV2 | ConversionOptionsV3 {
    try {
      return normalizeConversionOptions(rawOptions, { kind, allowLegacy: true });
    } catch (error) {
      if (error instanceof ConversionOptionsError) {
        throw ApiError.badRequest(error.code as never, {
          ...error.details,
          reason: error.code,
        });
      }
      throw error;
    }
  }

  /**
   * Options to reuse on retry: the stored canonical payload, or the defaults
   * inferred from the document kind for conversions created before options
   * were persisted.
   */
  optionsForRow(row: ConversionWithRelations): ConversionOptionsV2 | ConversionOptionsV3 {
    const stored = row.optionsJson;
    if (stored) {
      try {
        const parsed = JSON.parse(stored) as Record<string, unknown>;
        const kind =
          parsed && typeof parsed === 'object' && Array.isArray((parsed as {spreadsheet?: unknown}).spreadsheet)
            ? 'spreadsheet'
            : 'pdf';
        return normalizeConversionOptions(parsed, { kind, allowLegacy: false, allowStamped: true });
      } catch {
        // fall through to defaults
      }
    }
    const kind = (row.document.mimeType ?? '').startsWith('image/') ? 'image' : 'pdf';
    return normalizeConversionOptions(undefined, { kind });
  }

  private safeFilename(original: string | undefined): string {
    const raw = (original ?? '').replace(/\\/g, '/').split('/').pop() ?? '';
    const cleaned = raw.replace(/[\u0000-\u001f\u007f]/g, '').trim().slice(0, 200);
    return cleaned.length > 0 ? cleaned : 'documento.pdf';
  }
}
