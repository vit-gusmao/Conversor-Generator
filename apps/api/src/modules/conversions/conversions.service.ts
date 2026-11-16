import { Inject, Injectable, Logger } from '@nestjs/common';
import type { Prisma } from '@prisma/client';
import type {
  ConversionListItem,
  Paginated,
  Preview,
  ResultSummary,
} from '@pdf2x/contracts';

import { ApiError } from '../../common/errors/api-error';
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
  ) {}

  /* ------------------------------- create -------------------------------- */

  async create(
    user: AuthenticatedUser,
    file: UploadedPdfFile | undefined,
    idempotencyKey?: string,
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
    const targetFormats = isImage ? ['txt'] : ['xlsx', 'txt', 'csv'];

    const sha256 = inspection.sha256;
    const storageKey = storageKeyForSha256(sha256, extension, user.id);
    const key = idempotencyKey?.trim();

    if (key) {
      const existing = await this.prisma.idempotencyKey.findUnique({
        where: { ownerId_key: { ownerId: user.id, key } },
      });
      if (existing) {
        if (existing.requestHash !== sha256) {
          throw ApiError.conflict(ERROR_CODES.IDEMPOTENT_REPLAY, {
            reason: 'payload_mismatch',
          });
        }
        const replayed = await this.findRow(existing.conversionId);
        if (replayed) {
          return this.queuedResponse(replayed, 'Conversão já recebida anteriormente.');
        }
      }
    }

    await this.storage.put(storageKey, file.buffer, mimeType);

    const queuedProgress = progressFloorForStatus('queued');
    const created = await this.prisma.$transaction(async (tx) => {
      const document = await tx.document.create({
        data: {
          filename,
          mimeType,
          sizeBytes: file.buffer.length,
          sha256,
          storageKey,
          pageCount: inspection.page_count > 0 ? inspection.page_count : null,
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
          validationJson: JSON.stringify(toValidationReport(inspection)),
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
            requestHash: sha256,
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
          options: {
            maxPages: this.config.uploadMaxPages,
            // The same OCR run feeds XLSX, TXT and CSV; images produce TXT.
            targetFormats,
          },
        },
        tx,
      );

      return { conversion: queued, runId: run.id, outboxId: outbox.id };
    });

    await this.events.record(created.conversion.id, {
      type: 'status',
      status: 'queued',
      stage: stageKeyForStatus('queued'),
      progress: queuedProgress,
      message: 'Conversão recebida e enfileirada.',
      runId: created.runId,
      payload: { filename, page_count: inspection.page_count },
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

    const attempt = (row.runs[0]?.attempt ?? 0) + 1;
    const progress = progressFloorForStatus('queued');

    const run = await this.prisma.conversionRun.create({
      data: {
        conversionId: row.id,
        attempt,
        status: 'queued',
        stage: stageKeyForStatus('queued'),
        progress,
        startedAt: new Date(),
      },
    });

    const updated = await this.prisma.conversion.update({
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

    await this.events.record(row.id, {
      type: 'status',
      status: 'queued',
      stage: stageKeyForStatus('queued'),
      progress,
      message: `Nova tentativa iniciada (tentativa ${attempt}).`,
      runId: run.id,
      payload: { attempt },
    });

    const outbox = await this.outbox.record(OUTBOX_ENQUEUE, {
      conversionId: row.id,
      runId: run.id,
      storageKey: row.document.storageKey,
      filename: row.document.filename,
      options: { maxPages: this.config.uploadMaxPages },
    });
    // dispatch() already persists failures, but it can still reject if the
    // failure write itself fails. Guard the process against an unhandled
    // rejection: the durable outbox row remains for the drainer.
    void this.outbox.dispatch(outbox.id).catch((error: unknown) => {
      this.logger.warn(`Outbox ${outbox.id} não pôde ser despachado: ${String(error)}`);
    });

    return this.queuedResponse(updated, 'Nova tentativa iniciada.');
  }

  async cancel(user: AuthenticatedUser, id: string): Promise<CancelConversionResponse> {
    const row = await this.findOwned(id, user);
    assertNotTerminal(row);

    if (row.activeRunId) {
      try {
        await this.converter.cancel(row.activeRunId);
      } catch (error) {
        // Cancelling is best effort on the engine side; the local state below
        // is authoritative for the user regardless.
        this.logger.warn(
          `Falha ao solicitar cancelamento de ${row.activeRunId}: ${String(error)}`,
        );
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

    return {
      conversion_id: row.id,
      status: 'cancelled',
      message: 'Cancelamento solicitado.',
      links: linksFor(row.id),
    };
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

  private safeFilename(original: string | undefined): string {
    const raw = (original ?? '').replace(/\\/g, '/').split('/').pop() ?? '';
    const cleaned = raw.replace(/[\u0000-\u001f\u007f]/g, '').trim().slice(0, 200);
    return cleaned.length > 0 ? cleaned : 'documento.pdf';
  }
}
