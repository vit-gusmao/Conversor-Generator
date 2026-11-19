/**
 * Unit-level harness for the batch and resumable upload services.
 *
 * Real services, real local storage in a temp directory, and the in-memory
 * Prisma double. Only the conversion engine boundary is stubbed: a stored
 * conversion becomes a `conversions` row, so batch roll-ups read real state.
 */
import { createHash } from 'node:crypto';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { Readable } from 'node:stream';

import { normalizeConversionOptions } from '@pdf2x/contracts';

import type { AuthenticatedUser } from '../../src/common/request-context';
import { BatchEventsService } from '../../src/modules/batches/batch-events.service';
import { BatchesService } from '../../src/modules/batches/batches.service';
import { UploadsService } from '../../src/modules/uploads/uploads.service';
import { FileValidationService } from '../../src/upload/file-validation.service';
import { LocalStorageService } from '../../src/upload/local-storage.service';

import { FakePrisma } from './fake-prisma';

export const OWNER: AuthenticatedUser = {
  id: 'owner-harness',
  email: null,
  name: null,
  role: 'owner',
  isAnonymous: true,
  createdAt: new Date(0).toISOString(),
  deviceId: null,
};

export interface Harness {
  prisma: FakePrisma;
  storage: LocalStorageService;
  storageRoot: string;
  events: BatchEventsService;
  batches: BatchesService;
  uploads: UploadsService;
  conversions: {
    normalizeOptions: jest.Mock;
    createStoredConversion: jest.Mock;
    retry: jest.Mock;
    cancel: jest.Mock;
  };
  dispose(): void;
}

export function createHarness(partBytes = 64): Harness {
  const storageRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'pdf2x-unit-'));
  const prisma = new FakePrisma();
  const config = {
    batchUploadEnabled: true,
    maxFileBytes: null,
    uploadPartBytes: partBytes,
    uploadSessionTtlHours: 24,
  } as never;

  let conversionSeq = 0;
  const conversions = {
    normalizeOptions: jest.fn((raw: unknown, kind: 'pdf' | 'image' | 'spreadsheet') =>
      normalizeConversionOptions((raw ?? undefined) as never, {
        kind,
        allowLegacy: true,
      } as never),
    ),
    createStoredConversion: jest.fn(
      async (_user: AuthenticatedUser, source: { filename: string }, options: unknown, meta: { idempotencyKey?: string }) => {
        const existing = meta.idempotencyKey
          ? prisma.conversions.find((row) => row['idempotencyKey'] === meta.idempotencyKey)
          : undefined;
        if (existing) return { conversion_id: existing['id'] as string };
        conversionSeq += 1;
        const id = `conv-${conversionSeq}`;
        prisma.conversions.push({
          id,
          status: 'queued',
          filename: source.filename,
          optionsJson: JSON.stringify(options),
          idempotencyKey: meta.idempotencyKey,
          ownerId: OWNER.id,
        });
        return { conversion_id: id };
      },
    ),
    retry: jest.fn(async (_user: AuthenticatedUser, id: string) => {
      const row = prisma.conversions.find((candidate) => candidate['id'] === id);
      if (row) row['status'] = 'queued';
      return { conversion_id: id, status: 'queued' };
    }),
    cancel: jest.fn(async (_user: AuthenticatedUser, id: string) => {
      const row = prisma.conversions.find((candidate) => candidate['id'] === id);
      if (row) row['status'] = 'cancelled';
      return { conversion_id: id, status: 'cancelled' };
    }),
  };

  const storage = new LocalStorageService({ storageLocalRoot: storageRoot } as never);
  const validation = new FileValidationService({ maxBytes: 64 * 1024 * 1024, maxPages: null });
  const events = new BatchEventsService(prisma as never);
  const batches = new BatchesService(prisma as never, config, events, conversions as never);
  const uploads = new UploadsService(
    prisma as never,
    config,
    validation,
    storage,
    conversions as never,
    batches,
  );

  return {
    prisma,
    storage,
    storageRoot,
    events,
    batches,
    uploads,
    conversions,
    dispose(): void {
      fs.rmSync(storageRoot, { recursive: true, force: true });
    },
  };
}

/** Registers one item and returns its upload id, item id and part size. */
export async function registerUpload(
  h: Harness,
  batchId: string,
  clientItemId: string,
  filename: string,
  bytes: Buffer,
  options?: Record<string, unknown> | null,
): Promise<{ uploadId: string; itemId: string; partSize: number }> {
  const registered = await h.batches.registerItem(OWNER, batchId, {
    client_item_id: clientItemId,
    filename,
    size_bytes: bytes.length,
    options: options ?? null,
  } as never);
  return {
    uploadId: registered.upload_id as string,
    itemId: registered.item.item_id,
    partSize: registered.part_size,
  };
}

/** Sends every part of `bytes` through the upload service, then completes it. */
export async function uploadAndComplete(
  h: Harness,
  uploadId: string,
  bytes: Buffer,
  partSize: number,
): Promise<{ conversion_id: string; replayed: boolean }> {
  const totalParts = Math.ceil(bytes.length / partSize);
  for (let part = 0; part < totalParts; part += 1) {
    const chunk = bytes.subarray(part * partSize, (part + 1) * partSize);
    await h.uploads.putPart(
      OWNER,
      uploadId,
      part,
      Readable.from([chunk]),
      createHash('sha256').update(chunk).digest('hex'),
      chunk.length,
    );
  }
  const sha = createHash('sha256').update(bytes).digest('hex');
  return h.uploads.complete(OWNER, uploadId, sha, totalParts) as Promise<{
    conversion_id: string;
    replayed: boolean;
  }>;
}

/** A PNG-signed payload, large enough for any single part. */
export function pngBytes(size = 80): Buffer {
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    Buffer.alloc(Math.max(0, size - 8), 0),
  ]);
}

/** Lists every regular file under `root` (relative paths). */
export function listFiles(root: string): string[] {
  if (!fs.existsSync(root)) return [];
  const out: string[] = [];
  const walk = (dir: string): void => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else out.push(path.relative(root, full));
    }
  };
  walk(root);
  return out;
}
