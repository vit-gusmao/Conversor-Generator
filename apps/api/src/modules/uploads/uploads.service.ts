import { createHash, randomUUID } from 'node:crypto';
import { Transform, type Readable } from 'node:stream';

import { Inject, Injectable, Logger } from '@nestjs/common';
import {
  DEFAULT_UPLOAD_PART_BYTES,
  type CompleteUploadResponse,
  type UploadPartReceipt,
  type UploadSessionDetail,
} from '@pdf2x/contracts';
import type { Prisma } from '@prisma/client';

import { ApiError } from '../../common/errors/api-error';
import { ERROR_CODES } from '../../common/errors/error-codes';
import type { AuthenticatedUser } from '../../common/request-context';
import { AppConfigService } from '../../config/app-config.service';
import { PrismaService } from '../../database/prisma.service';
import {
  STORAGE_SERVICE,
  storageKeyForSha256,
  uploadIncomingKey,
  uploadPartKey,
  uploadStagingKey,
  type StorageService,
} from '../../upload/storage.service';
import { FileValidationService } from '../../upload/file-validation.service';
import { BatchesService } from '../batches/batches.service';
import { ConversionsService } from '../conversions/conversions.service';
import { normalizeConversionOptions } from '@pdf2x/contracts';

const EXTENSION_FOR_IMAGE: Record<string, string> = {
  JPEG: 'jpg',
  JPEG2000: 'jp2',
  TIFF: 'tif',
};

interface UploadRow {
  id: string;
  ownerId: string;
  itemId: string;
  filename: string;
  expectedBytes: number;
  receivedBytes: number;
  partSize: number;
  sha256: string | null;
  state: string;
  expiresAt: Date;
  storageKey: string | null;
  createdAt: Date;
  updatedAt: Date;
}

interface PartRow {
  partNumber: number;
  size: number;
  sha256: string;
  storageKey: string;
}

interface ItemRow {
  id: string;
  batchId: string;
  filename: string;
  kind: string;
  optionsJson: string;
  conversionId: string | null;
}

function expectedParts(expectedBytes: number, partSize: number): number {
  if (expectedBytes <= 0) return 0;
  return Math.ceil(expectedBytes / partSize);
}

/**
 * Resumable upload transport.
 *
 * Parts are written as they arrive and assembled once at `complete`. The part
 * size is a transport parameter; nothing here caps the document size or the
 * page count.
 */
@Injectable()
export class UploadsService {
  private readonly logger = new Logger(UploadsService.name);

  constructor(
    private readonly prisma: PrismaService,
    private readonly config: AppConfigService,
    private readonly validation: FileValidationService,
    @Inject(STORAGE_SERVICE) private readonly storage: StorageService,
    private readonly conversions: ConversionsService,
    private readonly batches: BatchesService,
  ) {}

  private async load(user: AuthenticatedUser, uploadId: string): Promise<UploadRow> {
    const row = (await this.prisma.uploadSession.findUnique({
      where: { id: uploadId },
    })) as UploadRow | null;
    if (!row) throw ApiError.notFound({ resource: 'upload' });
    if (row.ownerId !== user.id) throw ApiError.forbidden({ resource: 'upload' });
    return row;
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

  private async partsCount(uploadId: string): Promise<number> {
    return this.prisma.uploadPart.count({ where: { uploadId } });
  }

  /* --------------------------------- read ----------------------------- */

  async get(
    user: AuthenticatedUser,
    uploadId: string,
    query: { cursor?: string | null; limit?: number },
  ): Promise<UploadSessionDetail> {
    const upload = await this.load(user, uploadId);
    const limit = Math.min(Math.max(query.limit ?? 100, 1), 1000);
    const cursorValue = query.cursor ? Number.parseInt(query.cursor, 10) : Number.NaN;

    const rows = (await this.prisma.uploadPart.findMany({
      where: {
        uploadId,
        ...(Number.isFinite(cursorValue) ? { partNumber: { gt: cursorValue } } : {}),
      },
      orderBy: { partNumber: 'asc' },
      take: limit + 1,
    })) as PartRow[];

    const hasMore = rows.length > limit;
    const page = hasMore ? rows.slice(0, limit) : rows;
    const count = await this.partsCount(uploadId);
    const next = await this.nextPart(uploadId);

    return {
      upload_id: upload.id,
      item_id: upload.itemId,
      filename: upload.filename,
      state: upload.state as UploadSessionDetail['state'],
      expected_bytes: upload.expectedBytes,
      received_bytes: upload.receivedBytes,
      sha256: upload.sha256,
      part_size: upload.partSize || DEFAULT_UPLOAD_PART_BYTES,
      parts_received: count,
      next_part: next,
      expires_at: upload.expiresAt.toISOString(),
      created_at: upload.createdAt.toISOString(),
      updated_at: upload.updatedAt.toISOString(),
      parts: page.map((row) => ({
        part_number: row.partNumber,
        size: row.size,
        sha256: row.sha256,
      })),
      next_cursor: hasMore && page.length > 0 ? String(page[page.length - 1].partNumber) : null,
    };
  }

  /* --------------------------------- parts ---------------------------- */

  async putPart(
    user: AuthenticatedUser,
    uploadId: string,
    partNumber: number,
    stream: Readable,
    declaredSha256: string | undefined,
    contentLength: number | null,
  ): Promise<UploadPartReceipt> {
    const upload = await this.load(user, uploadId);
    if (upload.state === 'completed' || upload.state === 'aborted') {
      throw ApiError.conflict(ERROR_CODES.UPLOAD_STATE_CONFLICT, { state: upload.state });
    }
    if (upload.expiresAt.getTime() < Date.now()) {
      throw ApiError.conflict(ERROR_CODES.UPLOAD_EXPIRED, {
        expires_at: upload.expiresAt.toISOString(),
      });
    }

    const partSize = upload.partSize || this.config.uploadPartBytes || DEFAULT_UPLOAD_PART_BYTES;
    const maxParts = expectedParts(upload.expectedBytes, partSize);
    if (!Number.isInteger(partNumber) || partNumber < 0 || (maxParts > 0 && partNumber >= maxParts)) {
      throw ApiError.badRequest(ERROR_CODES.UPLOAD_PART_MISMATCH, {
        part_number: partNumber,
        max_parts: maxParts,
      });
    }
    if (contentLength !== null && contentLength > partSize) {
      throw ApiError.badRequest(ERROR_CODES.FILE_TOO_LARGE, {
        part_size: partSize,
        content_length: contentLength,
      });
    }

    const existing = (await this.prisma.uploadPart.findUnique({
      where: { uploadId_partNumber: { uploadId, partNumber } },
    })) as PartRow | null;

    // Write under a unique key first: a divergent retransmission must never
    // overwrite an already-confirmed part.
    const incomingKey = uploadIncomingKey(uploadId, partNumber, randomUUID());
    const hash = createHash('sha256');
    let size = 0;
    const counter = new Transform({
      transform(chunk: Buffer, _encoding, callback) {
        hash.update(chunk);
        size += chunk.length;
        callback(null, chunk);
      },
    });

    try {
      await this.storage.putStream(incomingKey, stream.pipe(counter), 'application/octet-stream');
    } catch (error) {
      this.logger.warn(`Falha ao receber a parte ${partNumber}: ${String(error)}`);
      await this.storage.delete(incomingKey).catch(() => undefined);
      throw ApiError.storageUnavailable({ reason: 'part_write_failed' });
    }
    const digest = hash.digest('hex');

    if (size > partSize) {
      await this.storage.delete(incomingKey);
      throw ApiError.badRequest(ERROR_CODES.FILE_TOO_LARGE, {
        part_size: partSize,
        received: size,
      });
    }
    if (declaredSha256 && declaredSha256.toLowerCase() !== digest) {
      await this.storage.delete(incomingKey);
      throw ApiError.validation({
        code: ERROR_CODES.UPLOAD_CHECKSUM_MISMATCH,
        part_number: partNumber,
        expected: declaredSha256.toLowerCase(),
        received: digest,
      });
    }

    if (existing) {
      if (existing.sha256 === digest && existing.size === size) {
        await this.storage.delete(incomingKey);
        return {
          upload_id: uploadId,
          part_number: partNumber,
          size,
          sha256: digest,
          received_bytes: upload.receivedBytes,
          parts_received: await this.partsCount(uploadId),
          next_part: await this.nextPart(uploadId),
          replayed: true,
        };
      }
      await this.storage.delete(incomingKey);
      throw ApiError.conflict(ERROR_CODES.UPLOAD_PART_MISMATCH, { part_number: partNumber });
    }

    const partKey = uploadPartKey(uploadId, partNumber);
    await this.storage.move(incomingKey, partKey);

    try {
      await this.prisma.$transaction(async (tx: Prisma.TransactionClient) => {
        await tx.uploadPart.create({
          data: { uploadId, partNumber, size, sha256: digest, storageKey: partKey },
        });
        await tx.uploadSession.update({
          where: { id: uploadId },
          data: { receivedBytes: { increment: size }, state: 'uploading' },
        });
      });
    } catch (error) {
      // Lost a race for the same part number: the winner's row is authoritative.
      const winner = (await this.prisma.uploadPart.findUnique({
        where: { uploadId_partNumber: { uploadId, partNumber } },
      })) as PartRow | null;
      if (winner) {
        await this.storage.delete(partKey).catch(() => undefined);
        if (winner.sha256 !== digest || winner.size !== size) {
          throw ApiError.conflict(ERROR_CODES.UPLOAD_PART_MISMATCH, { part_number: partNumber });
        }
        const fresh = await this.load(user, uploadId);
        return {
          upload_id: uploadId,
          part_number: partNumber,
          size,
          sha256: digest,
          received_bytes: fresh.receivedBytes,
          parts_received: await this.partsCount(uploadId),
          next_part: await this.nextPart(uploadId),
          replayed: true,
        };
      }
      throw error;
    }

    const fresh = await this.load(user, uploadId);
    return {
      upload_id: uploadId,
      part_number: partNumber,
      size,
      sha256: digest,
      received_bytes: fresh.receivedBytes,
      parts_received: await this.partsCount(uploadId),
      next_part: await this.nextPart(uploadId),
      replayed: false,
    };
  }

  /* -------------------------------- complete -------------------------- */

  async complete(
    user: AuthenticatedUser,
    uploadId: string,
    sha256: string,
    totalParts: number,
  ): Promise<CompleteUploadResponse> {
    const expected = (sha256 ?? '').trim().toLowerCase();
    if (!/^[a-f0-9]{64}$/.test(expected)) {
      throw ApiError.validation({ reason: 'invalid_sha256' });
    }

    const upload = await this.load(user, uploadId);
    const item = (await this.prisma.batchItem.findUnique({
      where: { id: upload.itemId },
    })) as ItemRow | null;
    if (!item) throw ApiError.notFound({ resource: 'batch_item' });

    // The item is the authority on "already converted": a crash between
    // creating the conversion and marking the session complete must not let a
    // retry create a second one.
    if (item.conversionId) {
      return {
        upload_id: upload.id,
        item_id: item.id,
        conversion_id: item.conversionId,
        status: 'queued',
        message: 'Upload já concluído anteriormente.',
        replayed: true,
      };
    }
    if (upload.state === 'aborted') {
      throw ApiError.conflict(ERROR_CODES.UPLOAD_STATE_CONFLICT, { state: upload.state });
    }

    const partSize = upload.partSize || this.config.uploadPartBytes;
    const expectedCount = expectedParts(upload.expectedBytes, partSize);
    if (totalParts !== expectedCount) {
      throw ApiError.conflict(ERROR_CODES.UPLOAD_INCOMPLETE, {
        expected_parts: expectedCount,
        received_parts: totalParts,
      });
    }

    const parts = (await this.prisma.uploadPart.findMany({
      where: { uploadId },
      orderBy: { partNumber: 'asc' },
    })) as PartRow[];

    const numbers = parts.map((part) => part.partNumber);
    const contiguous = numbers.length === expectedCount && numbers.every((value, index) => value === index);
    if (!contiguous) {
      const present = new Set(numbers);
      const missing: number[] = [];
      for (let index = 0; index < expectedCount; index += 1) {
        if (!present.has(index)) missing.push(index);
      }
      throw ApiError.conflict(ERROR_CODES.UPLOAD_INCOMPLETE, {
        missing_parts: missing.slice(0, 50),
        received_parts: numbers.length,
      });
    }

    const totalBytes = parts.reduce((sum, part) => sum + part.size, 0);
    if (totalBytes !== upload.expectedBytes) {
      throw ApiError.conflict(ERROR_CODES.UPLOAD_INCOMPLETE, {
        received_bytes: totalBytes,
        expected_bytes: upload.expectedBytes,
      });
    }

    const stagingKey = uploadStagingKey(uploadId);
    const assembled = await this.storage.compose(
      parts.map((part) => part.storageKey),
      stagingKey,
      'application/octet-stream',
    );

    // Hash the assembled object by streaming it back, never buffering it.
    const digest = createHash('sha256');
    let verifiedBytes = 0;
    const verifyStream = await this.storage.openRead(stagingKey);
    for await (const chunk of verifyStream) {
      const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk as Uint8Array);
      digest.update(buffer);
      verifiedBytes += buffer.length;
    }
    const assembledSha = digest.digest('hex');

    if (assembledSha !== expected) {
      await this.storage.delete(stagingKey).catch(() => undefined);
      throw ApiError.validation({
        code: ERROR_CODES.UPLOAD_CHECKSUM_MISMATCH,
        expected,
        received: assembledSha,
      });
    }

    // Content detection from the header only: a `.pdf` name never decides.
    // Declared spreadsheet items (xlsx/csv) are admitted by declaration here
    // and fully validated by the engine (OOXML markers / parsed dialect).
    const headStream = await this.storage.openRead(stagingKey);
    const head = await readHead(headStream, 1024);
    const inspection = this.validation.inspectHeader(head, item.kind);
    if (inspection.kind === 'unknown') {
      await this.storage.delete(stagingKey).catch(() => undefined);
      throw ApiError.badRequest(ERROR_CODES.INVALID_FILE, { reason: 'invalid_magic_bytes' });
    }

    const pageCount =
      inspection.kind === 'pdf' ? await this.countPages(stagingKey) : null;

    const extension =
      inspection.kind === 'pdf'
        ? 'pdf'
        : inspection.kind === 'spreadsheet'
          ? (inspection.spreadsheet_format ?? 'xlsx') === 'csv'
            ? 'csv'
            : 'xlsx'
          : (EXTENSION_FOR_IMAGE[inspection.image_format ?? ''] ?? 'img');
    const finalKey = storageKeyForSha256(expected, extension, user.id);
    if (await this.storage.exists(finalKey)) {
      await this.storage.delete(stagingKey).catch(() => undefined);
    } else {
      await this.storage.move(stagingKey, finalKey);
    }

    const options = normalizeConversionOptions(JSON.parse(item.optionsJson) as unknown, {
      kind:
        inspection.kind === 'image'
          ? 'image'
          : inspection.kind === 'spreadsheet'
            ? 'spreadsheet'
            : 'pdf',
      allowLegacy: false,
      allowStamped: true,
    });

    const accepted = await this.conversions.createStoredConversion(
      user,
      {
        storageKey: finalKey,
        sha256: expected,
        sizeBytes: verifiedBytes,
        filename: item.filename,
        mimeType: inspection.mime_type ?? 'application/octet-stream',
        pageCount,
        validation: {
          ok: true,
          is_pdf: inspection.kind === 'pdf',
          pdf_version: inspection.pdf_version,
          page_count: pageCount ?? 0,
          encrypted: false,
          needs_password: false,
          has_forms: false,
          has_javascript: false,
          has_embedded_files: false,
          has_launch_actions: false,
          rotation_pages: [],
          warnings: [],
          errors: [],
        },
      },
      options,
      // A stable key makes "one upload = one conversion" a database constraint.
      { idempotencyKey: `upload:${upload.id}` },
    );

    await this.prisma.uploadSession.update({
      where: { id: uploadId },
      data: {
        state: 'completed',
        sha256: expected,
        storageKey: finalKey,
        receivedBytes: totalBytes,
      },
    });

    await this.batches.markItemQueued(item.batchId, item.id, accepted.conversion_id, pageCount);

    return {
      upload_id: upload.id,
      item_id: item.id,
      conversion_id: accepted.conversion_id,
      status: 'queued',
      message: 'Upload concluído e conversão enfileirada.',
      replayed: false,
    };
  }

  private async countPages(key: string): Promise<number | null> {
    const stream = await this.storage.openRead(key);
    const count = await this.validation.countPagesStreaming(stream);
    return count > 0 ? count : null;
  }

  /* -------------------------------- abort ----------------------------- */

  async abort(user: AuthenticatedUser, uploadId: string): Promise<Record<string, unknown>> {
    const upload = await this.load(user, uploadId);
    if (upload.state === 'completed') {
      throw ApiError.conflict(ERROR_CODES.UPLOAD_STATE_CONFLICT, { state: upload.state });
    }

    const parts = (await this.prisma.uploadPart.findMany({ where: { uploadId } })) as PartRow[];
    for (const part of parts) {
      await this.storage.delete(part.storageKey).catch(() => undefined);
    }
    await this.prisma.uploadPart.deleteMany({ where: { uploadId } });

    const item = (await this.prisma.batchItem.findUnique({
      where: { id: upload.itemId },
    })) as ItemRow | null;
    if (item) {
      const terminal = ['completed', 'completed_with_warnings', 'failed', 'cancelled'];
      await this.prisma.batchItem.updateMany({
        where: { id: item.id, status: { notIn: terminal } },
        data: {
          status: 'upload_failed',
          phase: 'upload',
          errorJson: JSON.stringify({
            code: 'UPLOAD_ABORTED',
            message: 'O envio deste arquivo foi interrompido.',
          }),
          uploadId: null,
        },
      });
    }

    await this.prisma.uploadSession.update({
      where: { id: uploadId },
      data: { state: 'aborted', receivedBytes: 0 },
    });

    return { upload_id: uploadId, state: 'aborted', deleted_parts: parts.length };
  }
}

async function readHead(stream: Readable, bytes: number): Promise<Buffer> {
  const chunks: Buffer[] = [];
  let total = 0;
  for await (const chunk of stream) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk as Uint8Array);
    const remaining = bytes - total;
    if (remaining <= 0) break;
    chunks.push(buffer.subarray(0, remaining));
    total += Math.min(buffer.length, remaining);
    if (total >= bytes) break;
  }
  stream.destroy?.();
  return Buffer.concat(chunks);
}
