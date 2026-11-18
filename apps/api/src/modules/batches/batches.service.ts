import { Injectable, Logger } from '@nestjs/common';
import {
  BATCH_LIST_DEFAULT_LIMIT,
  BATCH_LIST_MAX_LIMIT,
  TERMINAL_ITEM_STATUSES,
  canonicalOptionsJson,
  normalizeConversionOptions,
  type BatchCounts,
  type BatchItem,
  type BatchItemStatus,
  type BatchProgress,
  type BatchStatus,
  type BatchSummary,
  type ConversionOptionsV2,
  type CursorPage,
} from '@pdf2x/contracts';
import type { Prisma } from '@prisma/client';

import { ApiError } from '../../common/errors/api-error';
import { ERROR_CODES } from '../../common/errors/error-codes';
import { decodeCursor, encodeCursor, requestFingerprint } from '../../common/fingerprint';
import type { AuthenticatedUser } from '../../common/request-context';
import { AppConfigService } from '../../config/app-config.service';
import { PrismaService } from '../../database/prisma.service';
import { parseJson } from '../../database/json.util';
import { ConversionsService } from '../conversions/conversions.service';

import { BatchEventsService } from './batch-events.service';
import type { RegisterBatchItemDto } from './dto/batch.dto';

/** Upload-phase statuses are stored; the rest are derived from the conversion. */
const UPLOAD_PHASE_STATUSES: readonly BatchItemStatus[] = ['pending_upload', 'uploading'];

const ACTIVE_CONVERSION_STATUSES = [
  'received',
  'validating',
  'queued',
  'inspecting',
  'extracting',
  'structuring',
  'normalizing',
  'reconciling',
  'generating',
  'auditing',
];

const IMAGE_EXTENSIONS = new Set([
  'png',
  'jpg',
  'jpeg',
  'bmp',
  'webp',
  'tif',
  'tiff',
  'gif',
  'ico',
  'jp2',
  'pnm',
  'avif',
  'heic',
  'heif',
]);

interface BatchRow {
  id: string;
  ownerId: string;
  clientBatchKey: string;
  status: string;
  version: number;
  createdAt: Date;
  sealedAt: Date | null;
  cancelRequestedAt: Date | null;
}

interface BatchItemRow {
  id: string;
  batchId: string;
  clientItemId: string;
  ordinal: number;
  filename: string;
  sizeBytes: number;
  kind: string;
  optionsJson: string;
  requestFingerprint: string;
  uploadId: string | null;
  conversionId: string | null;
  status: string;
  errorJson: string | null;
  pagesTotal: number | null;
  pagesAttempted: number | null;
  pagesCompleted: number | null;
  pagesFailed: number | null;
  phase: string | null;
  createdAt: Date;
  updatedAt: Date;
  conversion?: { status: string } | null;
}

function declaredKind(filename: string): 'pdf' | 'image' | 'unknown' {
  const index = filename.lastIndexOf('.');
  if (index < 0) return 'unknown';
  const suffix = filename.slice(index + 1).toLowerCase();
  if (suffix === 'pdf') return 'pdf';
  if (IMAGE_EXTENSIONS.has(suffix)) return 'image';
  return 'unknown';
}

function effectiveStatus(row: BatchItemRow): BatchItemStatus {
  if (row.status === 'cancelled') return 'cancelled';
  if ((UPLOAD_PHASE_STATUSES as readonly string[]).includes(row.status)) {
    return row.status as BatchItemStatus;
  }
  if (row.status === 'upload_failed') return 'upload_failed';
  const conversionStatus = row.conversion?.status ?? null;
  if (!conversionStatus) return row.conversionId ? 'queued' : (row.status as BatchItemStatus);
  if ((TERMINAL_ITEM_STATUSES as readonly string[]).includes(conversionStatus)) {
    return conversionStatus as BatchItemStatus;
  }
  if (conversionStatus === 'queued') return 'queued';
  return 'processing';
}

function batchStatus(
  counts: BatchCounts,
  sealed: boolean,
  cancelRequested: boolean,
): BatchStatus {
  const total = counts.total_items;
  if (cancelRequested && total > 0 && counts.cancelled === total) return 'cancelled';
  if (!sealed) return 'accepting';
  const terminal = counts.completed + counts.with_warnings + counts.failed + counts.cancelled;
  if (terminal >= total) {
    const problems = counts.failed + counts.cancelled + counts.with_warnings;
    return problems > 0 ? 'completed_with_errors' : 'completed';
  }
  return 'processing';
}

function emptyCounts(): BatchCounts {
  return {
    total_items: 0,
    pending_upload: 0,
    uploading: 0,
    queued: 0,
    processing: 0,
    completed: 0,
    with_warnings: 0,
    failed: 0,
    cancelled: 0,
  };
}

function itemLinks(batchId: string, itemId: string, conversionId: string | null): Record<string, string> {
  const base = `/api/v1/batches/${batchId}/items/${itemId}`;
  const links: Record<string, string> = {
    cancel: `${base}/cancel`,
    retry: `${base}/retry`,
  };
  if (conversionId) {
    links.conversion = `/api/v1/conversions/${conversionId}`;
    links.result = `/api/v1/conversions/${conversionId}/result`;
  }
  return links;
}

/**
 * Batch lifecycle: a durable container of independent documents.
 *
 * No product ceiling is applied to the number of items. Pagination limits are
 * transport parameters; a batch with 1001 items is listed in pages, never
 * rejected.
 */
@Injectable()
export class BatchesService {
  private readonly logger = new Logger(BatchesService.name);

  constructor(
    private readonly prisma: PrismaService,
    private readonly config: AppConfigService,
    private readonly events: BatchEventsService,
    private readonly conversions: ConversionsService,
  ) {}

  /* ---------------------------------- create -------------------------- */

  async create(user: AuthenticatedUser, clientBatchKey: string): Promise<BatchSummary> {
    if (!this.config.batchUploadEnabled) {
      throw ApiError.badRequest(ERROR_CODES.INVALID_OPTION, { reason: 'batch_disabled' });
    }
    const key = (clientBatchKey ?? '').trim();
    if (key.length === 0 || key.length > 128) {
      throw ApiError.badRequest(ERROR_CODES.INVALID_OPTION, {
        reason: 'invalid_client_batch_key',
      });
    }

    const existing = (await this.prisma.batch.findUnique({
      where: { ownerId_clientBatchKey: { ownerId: user.id, clientBatchKey: key } },
    })) as BatchRow | null;
    if (existing) return this.summary(existing);

    try {
      const created = (await this.prisma.batch.create({
        data: { ownerId: user.id, clientBatchKey: key, status: 'accepting', version: 1 },
      })) as BatchRow;
      await this.events.record(created.id, {
        type: 'batch',
        status: 'accepting',
        message: 'Lote criado.',
      });
      return this.summary(created);
    } catch (error) {
      // Concurrent creation with the same key: replay the winner.
      const winner = (await this.prisma.batch.findUnique({
        where: { ownerId_clientBatchKey: { ownerId: user.id, clientBatchKey: key } },
      })) as BatchRow | null;
      if (winner) return this.summary(winner);
      throw error;
    }
  }

  /* ----------------------------------- read --------------------------- */

  private async load(user: AuthenticatedUser, batchId: string): Promise<BatchRow> {
    const row = (await this.prisma.batch.findUnique({
      where: { id: batchId },
    })) as BatchRow | null;
    if (!row) throw ApiError.notFound({ resource: 'batch' });
    if (row.ownerId !== user.id) throw ApiError.forbidden({ resource: 'batch' });
    return row;
  }

  async get(user: AuthenticatedUser, batchId: string): Promise<BatchSummary> {
    const row = await this.load(user, batchId);
    const refreshed = await this.refreshStatus(row);
    return this.summary(refreshed);
  }

  private async refreshStatus(row: BatchRow): Promise<BatchRow> {
    const counts = await this.counts(row.id);
    const status = batchStatus(
      counts,
      row.sealedAt !== null,
      row.cancelRequestedAt !== null,
    );
    if (status === row.status) return row;
    const updated = (await this.prisma.batch.update({
      where: { id: row.id },
      data: { status, version: { increment: 1 } },
    })) as BatchRow;
    return updated;
  }

  private async counts(batchId: string): Promise<BatchCounts> {
    const stored = await this.prisma.batchItem.groupBy({
      by: ['status'],
      where: { batchId },
      _count: { _all: true },
    });
    // Items already counted as upload-phase or cancelled must not be counted
    // again through their conversion, or the totals double-count them.
    const byConversion = await this.prisma.conversion.groupBy({
      by: ['status'],
      where: {
        batchItem: { is: { batchId, status: { notIn: ['cancelled', 'upload_failed'] } } },
      },
      _count: { _all: true },
    });

    const counts = emptyCounts();
    let uploadPhase = 0;
    for (const row of stored) {
      const count = row._count._all;
      counts.total_items += count;
      if ((UPLOAD_PHASE_STATUSES as readonly string[]).includes(row.status)) {
        counts[row.status as 'pending_upload' | 'uploading'] += count;
        uploadPhase += count;
      } else if (row.status === 'upload_failed') {
        // Counts as failed for the user but stays resumable; not a terminal
        // conversion outcome, so it is tracked in the upload bucket.
        counts.failed -= 0;
      } else if (row.status === 'cancelled') {
        counts.cancelled += count;
      }
    }

    let conversionTotal = 0;
    for (const row of byConversion) {
      const count = row._count._all;
      conversionTotal += count;
      switch (row.status) {
        case 'completed':
          counts.completed += count;
          break;
        case 'completed_with_warnings':
          counts.with_warnings += count;
          break;
        case 'failed':
          counts.failed += count;
          break;
        case 'cancelled':
          counts.cancelled += count;
          break;
        case 'queued':
          counts.queued += count;
          break;
        default:
          if (ACTIVE_CONVERSION_STATUSES.includes(row.status)) counts.processing += count;
          break;
      }
    }

    // Items still uploading (or failed at upload) have no conversion yet.
    const withoutConversion = counts.total_items - uploadPhase - counts.cancelled - conversionTotal;
    if (withoutConversion > 0) counts.pending_upload += withoutConversion;

    return counts;
  }

  private async progress(batchId: string): Promise<BatchProgress> {
    const aggregate = await this.prisma.batchItem.aggregate({
      where: { batchId },
      _sum: {
        pagesTotal: true,
        pagesAttempted: true,
        pagesCompleted: true,
        pagesFailed: true,
      },
    });
    const counts = await this.counts(batchId);
    return {
      finished_items:
        counts.completed + counts.with_warnings + counts.failed + counts.cancelled,
      pages_total: aggregate._sum.pagesTotal ?? 0,
      pages_attempted: aggregate._sum.pagesAttempted ?? 0,
      pages_completed: aggregate._sum.pagesCompleted ?? 0,
      pages_failed: aggregate._sum.pagesFailed ?? 0,
    };
  }

  private async summary(row: BatchRow): Promise<BatchSummary> {
    const [counts, progress] = await Promise.all([this.counts(row.id), this.progress(row.id)]);
    return {
      batch_id: row.id,
      status: row.status as BatchStatus,
      client_batch_key: row.clientBatchKey,
      counts,
      progress,
      created_at: row.createdAt.toISOString(),
      sealed_at: row.sealedAt?.toISOString() ?? null,
      cancel_requested_at: row.cancelRequestedAt?.toISOString() ?? null,
      version: row.version,
      links: {
        self: `/api/v1/batches/${row.id}`,
        items: `/api/v1/batches/${row.id}/items`,
        events: `/api/v1/batches/${row.id}/events`,
        seal: `/api/v1/batches/${row.id}/seal`,
        cancel: `/api/v1/batches/${row.id}/cancel`,
      },
    };
  }

  /* ------------------------------- item registration ------------------ */

  async registerItem(
    user: AuthenticatedUser,
    batchId: string,
    input: RegisterBatchItemDto,
  ) {
    const row = await this.load(user, batchId);
    if (row.sealedAt) throw ApiError.conflict(ERROR_CODES.BATCH_NOT_OPEN, { batch_id: batchId });
    if (row.cancelRequestedAt) {
      throw ApiError.conflict(ERROR_CODES.BATCH_CANCELLED, { batch_id: batchId });
    }

    const clientItemId = (input.client_item_id ?? '').trim();
    if (clientItemId.length === 0 || clientItemId.length > 128) {
      throw ApiError.badRequest(ERROR_CODES.INVALID_OPTION, {
        reason: 'invalid_client_item_id',
      });
    }
    const filename = (input.filename ?? '').replace(/\\/g, '/').split('/').pop()?.trim() ?? '';
    if (!filename) throw ApiError.badRequest(ERROR_CODES.INVALID_FILE, { reason: 'missing_filename' });

    const size = Number(input.size_bytes);
    if (!Number.isFinite(size) || size <= 0) {
      throw ApiError.badRequest(ERROR_CODES.INVALID_FILE, { reason: 'empty_file' });
    }
    if (this.config.maxFileBytes !== null && size > this.config.maxFileBytes) {
      throw ApiError.badRequest(ERROR_CODES.FILE_TOO_LARGE, {
        max_bytes: this.config.maxFileBytes,
        size_bytes: size,
      });
    }

    const kind = declaredKind(filename);
    if (kind === 'unknown') {
      throw ApiError.badRequest(ERROR_CODES.INVALID_FILE, { reason: 'unsupported_extension' });
    }

    const options = this.normalizeOptions(input.options, kind);
    const fingerprint = this.itemFingerprint(options, filename, size);

    const existing = (await this.prisma.batchItem.findUnique({
      where: { batchId_clientItemId: { batchId, clientItemId } },
    })) as BatchItemRow | null;

    if (existing) {
      if (existing.requestFingerprint !== fingerprint) {
        throw ApiError.conflict(ERROR_CODES.IDEMPOTENT_REPLAY, {
          reason: 'payload_mismatch',
          item_id: existing.id,
        });
      }
      const upload = existing.uploadId
        ? await this.prisma.uploadSession.findUnique({ where: { id: existing.uploadId } })
        : null;
      return {
        item: this.itemPayload(existing),
        upload_id: existing.uploadId,
        part_size: this.config.uploadPartBytes,
        next_part: upload ? await this.nextPart(existing.uploadId as string) : 0,
        replayed: true,
        expires_at: upload?.expiresAt?.toISOString() ?? null,
      };
    }

    const highest = await this.prisma.batchItem.aggregate({
      where: { batchId },
      _max: { ordinal: true },
    });
    const ordinal = (highest._max.ordinal ?? 0) + 1;

    const expiresAt = new Date(
      Date.now() + this.config.uploadSessionTtlHours * 60 * 60 * 1000,
    );

    try {
      const created = await this.prisma.$transaction(async (tx) => {
        const item = (await tx.batchItem.create({
          data: {
            batchId,
            clientItemId,
            ordinal,
            filename,
            sizeBytes: size,
            kind,
            optionsJson: canonicalOptionsJson(options),
            requestFingerprint: fingerprint,
            status: 'pending_upload',
          },
        })) as BatchItemRow;

        const upload = await tx.uploadSession.create({
          data: {
            ownerId: user.id,
            itemId: item.id,
            filename,
            expectedBytes: size,
            receivedBytes: 0,
            partSize: this.config.uploadPartBytes,
            state: 'pending',
            expiresAt,
          },
        });

        const linked = (await tx.batchItem.update({
          where: { id: item.id },
          data: { uploadId: upload.id },
        })) as BatchItemRow;

        await tx.batch.update({ where: { id: batchId }, data: { version: { increment: 1 } } });
        return { item: linked, uploadId: upload.id };
      });

      await this.events.record(batchId, {
        type: 'item',
        itemId: created.item.id,
        status: row.status,
        itemStatus: 'pending_upload',
        message: `Item registrado: ${filename}.`,
        payload: { filename, size_bytes: size, kind },
      });

      return {
        item: this.itemPayload(created.item),
        upload_id: created.uploadId,
        part_size: this.config.uploadPartBytes,
        next_part: 0,
        replayed: false,
        expires_at: expiresAt.toISOString(),
      };
    } catch (error) {
      const winner = (await this.prisma.batchItem.findUnique({
        where: { batchId_clientItemId: { batchId, clientItemId } },
      })) as BatchItemRow | null;
      if (winner) {
        return {
          item: this.itemPayload(winner),
          upload_id: winner.uploadId,
          part_size: this.config.uploadPartBytes,
          next_part: winner.uploadId ? await this.nextPart(winner.uploadId) : 0,
          replayed: true,
          expires_at: null,
        };
      }
      throw error;
    }
  }

  private async nextPart(uploadId: string): Promise<number> {
    const highest = await this.prisma.uploadPart.aggregate({
      where: { uploadId },
      _max: { partNumber: true },
    });
    return highest._max.partNumber === null || highest._max.partNumber === undefined
      ? 0
      : highest._max.partNumber + 1;
  }

  async listItems(
    user: AuthenticatedUser,
    batchId: string,
    query: { cursor?: string | null; limit?: number; status?: string | null },
  ): Promise<CursorPage<BatchItem>> {
    await this.load(user, batchId);
    const limit = Math.min(
      Math.max(query.limit ?? BATCH_LIST_DEFAULT_LIMIT, 1),
      BATCH_LIST_MAX_LIMIT,
    );

    const where: Prisma.BatchItemWhereInput = { batchId };
    const decoded = decodeCursor(query.cursor ?? null);
    if (decoded) {
      const ordinal = typeof decoded['ordinal'] === 'number' ? decoded['ordinal'] : 0;
      const id = typeof decoded['id'] === 'string' ? decoded['id'] : '';
      where.OR = [
        { ordinal: { gt: ordinal } },
        { AND: [{ ordinal }, { id: { gt: id } }] },
      ];
    }

    const rows = (await this.prisma.batchItem.findMany({
      where,
      include: { conversion: { select: { status: true } } },
      orderBy: [{ ordinal: 'asc' }, { id: 'asc' }],
      take: limit + 1,
    })) as BatchItemRow[];

    const hasMore = rows.length > limit;
    const page = hasMore ? rows.slice(0, limit) : rows;
    let items = page.map((row) => this.itemPayload(row));
    if (query.status) items = items.filter((item) => item.status === query.status);

    const last = page[page.length - 1];
    return {
      items,
      next_cursor:
        hasMore && last ? encodeCursor({ ordinal: last.ordinal, id: last.id }) : null,
    };
  }

  itemPayload(row: BatchItemRow): BatchItem {
    const options = this.optionsOf(row);
    const status = effectiveStatus(row);
    const kind = row.kind === 'pdf' || row.kind === 'image' ? row.kind : 'unknown';
    return {
      item_id: row.id,
      batch_id: row.batchId,
      client_item_id: row.clientItemId,
      ordinal: row.ordinal,
      filename: row.filename,
      size_bytes: row.sizeBytes,
      kind,
      status,
      options,
      available_formats:
        kind === 'image'
          ? ['txt']
          : (options.target_formats as BatchItem['available_formats']),
      upload_id: row.uploadId,
      conversion_id: row.conversionId,
      pages_total: row.pagesTotal,
      pages_attempted: row.pagesAttempted,
      pages_completed: row.pagesCompleted,
      pages_failed: row.pagesFailed,
      phase: row.phase,
      error: parseJson<BatchItem['error']>(row.errorJson),
      created_at: row.createdAt.toISOString(),
      updated_at: row.updatedAt.toISOString(),
      links: itemLinks(row.batchId, row.id, row.conversionId),
    };
  }

  private optionsOf(row: BatchItemRow): ConversionOptionsV2 {
    const parsed = parseJson<ConversionOptionsV2>(row.optionsJson);
    if (parsed) {
      try {
        return normalizeConversionOptions(parsed, {
          kind: row.kind === 'image' ? 'image' : 'pdf',
          allowLegacy: false,
        });
      } catch {
        // fall through to the defaults
      }
    }
    return normalizeConversionOptions(undefined, {
      kind: row.kind === 'image' ? 'image' : 'pdf',
    });
  }

  private normalizeOptions(raw: unknown, kind: 'pdf' | 'image'): ConversionOptionsV2 {
    return this.conversions.normalizeOptions(raw, kind);
  }

  /* --------------------------------- seal ----------------------------- */

  async seal(user: AuthenticatedUser, batchId: string) {
    const row = await this.load(user, batchId);
    if (row.sealedAt) {
      return {
        batch_id: row.id,
        status: row.status as BatchStatus,
        sealed_at: row.sealedAt.toISOString(),
        message: 'O lote já estava selado.',
      };
    }

    // `upload_failed` is a definitive outcome and does not block sealing;
    // otherwise one abandoned upload would trap the whole batch.
    const pending = await this.prisma.batchItem.count({
      where: { batchId, status: { in: ['pending_upload', 'uploading'] } },
    });
    if (pending > 0) {
      throw ApiError.conflict(ERROR_CODES.BATCH_HAS_PENDING_UPLOADS, {
        pending_uploads: pending,
      });
    }

    const counts = await this.counts(batchId);
    const status = batchStatus(counts, true, row.cancelRequestedAt !== null);
    const updated = (await this.prisma.batch.update({
      where: { id: batchId },
      data: { sealedAt: new Date(), status, version: { increment: 1 } },
    })) as BatchRow;

    await this.events.record(batchId, {
      type: 'batch',
      status,
      message: 'Lote selado; novas inclusões estão bloqueadas.',
      payload: { counts },
    });

    return {
      batch_id: updated.id,
      status: updated.status as BatchStatus,
      sealed_at: updated.sealedAt?.toISOString() ?? new Date().toISOString(),
      message: 'Lote selado.',
    };
  }

  /* ------------------------------- cancel/retry ----------------------- */

  async cancel(user: AuthenticatedUser, batchId: string): Promise<BatchSummary> {
    const row = await this.load(user, batchId);
    if (!row.cancelRequestedAt) {
      await this.prisma.batch.update({
        where: { id: batchId },
        data: { cancelRequestedAt: new Date(), version: { increment: 1 } },
      });
    }

    const active = (await this.prisma.batchItem.findMany({
      where: { batchId, status: { notIn: [...TERMINAL_ITEM_STATUSES] } },
      select: { id: true, conversionId: true },
    })) as Array<{ id: string; conversionId: string | null }>;

    for (const item of active) {
      if (item.conversionId) {
        try {
          await this.conversions.cancel(user, item.conversionId);
        } catch (error) {
          this.logger.debug(`Cancelamento de ${item.conversionId} ignorado: ${String(error)}`);
        }
      }
      await this.prisma.batchItem.update({
        where: { id: item.id },
        data: { status: 'cancelled', phase: 'cancelled' },
      });
    }

    const counts = await this.counts(batchId);
    const status = batchStatus(counts, row.sealedAt !== null, true);
    const updated = (await this.prisma.batch.update({
      where: { id: batchId },
      data: { status, version: { increment: 1 } },
    })) as BatchRow;

    await this.events.record(batchId, {
      type: 'batch',
      status,
      message: 'Cancelamento do lote solicitado.',
      payload: { counts },
    });
    return this.summary(updated);
  }

  async cancelItem(user: AuthenticatedUser, batchId: string, itemId: string): Promise<BatchItem> {
    const batch = await this.load(user, batchId);
    const item = await this.loadItem(batchId, itemId);
    if ((TERMINAL_ITEM_STATUSES as readonly string[]).includes(item.status)) {
      return this.itemPayload(item);
    }

    if (item.conversionId) {
      try {
        await this.conversions.cancel(user, item.conversionId);
      } catch (error) {
        this.logger.debug(`Cancelamento de ${item.conversionId} ignorado: ${String(error)}`);
      }
    }

    const updated = (await this.prisma.batchItem.update({
      where: { id: itemId },
      data: { status: 'cancelled', phase: 'cancelled' },
    })) as BatchItemRow;
    await this.prisma.batch.update({
      where: { id: batch.id },
      data: { version: { increment: 1 } },
    });
    await this.events.record(batchId, {
      type: 'item',
      itemId,
      status: batch.status,
      itemStatus: 'cancelled',
      message: 'Item cancelado.',
    });
    return this.itemPayload(updated);
  }

  async retryItem(
    user: AuthenticatedUser,
    batchId: string,
    itemId: string,
    scope: 'upload' | 'conversion',
  ) {
    const batch = await this.load(user, batchId);
    const item = await this.loadItem(batchId, itemId);

    if (scope === 'upload') {
      if (!item.uploadId) {
        throw ApiError.conflict(ERROR_CODES.UPLOAD_NOT_FOUND, { item_id: itemId });
      }
      await this.prisma.uploadSession.update({
        where: { id: item.uploadId },
        data: {
          state: 'uploading',
          expiresAt: new Date(Date.now() + this.config.uploadSessionTtlHours * 3_600_000),
        },
      });
      const updated = (await this.prisma.batchItem.update({
        where: { id: itemId },
        data: { status: 'uploading', phase: 'upload', errorJson: null },
      })) as BatchItemRow;
      await this.events.record(batchId, {
        type: 'item',
        itemId,
        status: batch.status,
        itemStatus: 'uploading',
        message: 'Upload retomado.',
      });
      return {
        item: this.itemPayload(updated),
        upload_id: item.uploadId,
        next_part: await this.nextPart(item.uploadId),
        part_size: this.config.uploadPartBytes,
      };
    }

    if (!item.conversionId) {
      throw ApiError.conflict(ERROR_CODES.NOT_READY, { item_id: itemId });
    }
    if (!(TERMINAL_ITEM_STATUSES as readonly string[]).includes(item.status)) {
      throw ApiError.conflict(ERROR_CODES.CONVERSION_BUSY, { item_id: itemId });
    }

    const result = await this.conversions.retry(user, item.conversionId);
    const updated = (await this.prisma.batchItem.update({
      where: { id: itemId },
      data: { status: 'queued', phase: 'conversion' },
    })) as BatchItemRow;
    await this.events.record(batchId, {
      type: 'item',
      itemId,
      status: batch.status,
      itemStatus: 'queued',
      message: 'Nova tentativa de conversão iniciada.',
      payload: { conversion_id: result.conversion_id },
    });
    return { item: this.itemPayload(updated), conversion: result };
  }

  private async loadItem(batchId: string, itemId: string): Promise<BatchItemRow> {
    const item = (await this.prisma.batchItem.findUnique({
      where: { id: itemId },
      include: { conversion: { select: { status: true } } },
    })) as BatchItemRow | null;
    if (!item || item.batchId !== batchId) {
      throw ApiError.notFound({ resource: 'batch_item' });
    }
    return item;
  }

  /* ------------------------------ item updates ------------------------ */

  /** Called by the upload completion and by the tracker. */
  async markItemQueued(
    batchId: string,
    itemId: string,
    conversionId: string,
    pages: number | null,
  ): Promise<void> {
    await this.prisma.batchItem.update({
      where: { id: itemId },
      data: {
        conversionId,
        status: 'queued',
        phase: 'conversion',
        ...(pages !== null
          ? { pagesTotal: pages, pagesAttempted: 0, pagesCompleted: 0, pagesFailed: 0 }
          : {}),
      },
    });
    const batch = (await this.prisma.batch.findUnique({ where: { id: batchId } })) as BatchRow | null;
    await this.events.record(batchId, {
      type: 'item',
      itemId,
      status: batch?.status ?? 'processing',
      itemStatus: 'queued',
      message: 'Upload concluído e conversão enfileirada.',
      payload: { conversion_id: conversionId },
    });
  }

  /** Recomputes the batch roll-up after an item changed state. */
  async touchBatch(batchId: string): Promise<void> {
    const row = (await this.prisma.batch.findUnique({ where: { id: batchId } })) as BatchRow | null;
    if (!row) return;
    await this.refreshStatus(row);
  }

  private itemFingerprint(
    options: ConversionOptionsV2,
    filename: string,
    sizeBytes: number,
  ): string {
    return requestFingerprint(
      `${filename}\u0000${sizeBytes}`,
      options,
    );
  }
}
