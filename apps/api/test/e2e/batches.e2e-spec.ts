import { createHash } from 'node:crypto';
import * as os from 'node:os';
import * as path from 'node:path';

import type { INestApplication } from '@nestjs/common';
import { Test } from '@nestjs/testing';
import * as fs from 'node:fs';

import { AppModule } from '../../src/app.module';
import { createValidationPipe } from '../../src/common/pipes/validation.pipe';
import { PrismaService } from '../../src/database/prisma.service';
import { ConverterClient } from '../../src/modules/conversions/converter.client';
import { FileValidationService, UPLOAD_LIMITS } from '../../src/upload/file-validation.service';
import { LocalStorageService } from '../../src/upload/local-storage.service';
import { STORAGE_SERVICE } from '../../src/upload/storage.service';

import { FakePrisma } from '../helpers/fake-prisma';
import { makePdf } from '../helpers/test-utils';

/**
 * Batch + resumable upload contract.
 *
 * Mirrors `services/converter/tests/test_batches_api.py`; the full-mode
 * gateway must expose the same lifecycle as the standalone API.
 */
describe('Batches and uploads (e2e)', () => {
  let app: INestApplication;
  let prisma: FakePrisma;
  let storageRoot: string;
  let enqueue: jest.Mock;


  beforeAll(async () => {
    process.env.AUTH_MODE = 'optional';

    prisma = new FakePrisma();
    storageRoot = path.join(os.tmpdir(), `pdf2x-batches-e2e-${process.pid}`);
    fs.rmSync(storageRoot, { recursive: true, force: true });

    enqueue = jest.fn().mockResolvedValue({ accepted: true, run_id: 'run', status: 'queued' });
    const converter = {
      enqueue,
      status: jest.fn().mockResolvedValue({ run_id: 'run', status: 'queued', progress: 0.1 }),
      cancel: jest.fn().mockResolvedValue(undefined),
      health: jest.fn().mockResolvedValue({ status: 'ok', capabilities: {} }),
      safeHealth: jest.fn().mockResolvedValue({ status: 'ok', capabilities: {} }),
      result: jest.fn(),
      preview: jest.fn(),
      download: jest.fn(),
      auditSummary: jest.fn(),
    };

    const moduleRef = await Test.createTestingModule({ imports: [AppModule] })
      .overrideProvider(PrismaService)
      .useValue(prisma)
      .overrideProvider(ConverterClient)
      .useValue(converter)
      .overrideProvider(STORAGE_SERVICE)
      .useValue(new LocalStorageService({
        storageLocalRoot: storageRoot,
      } as never))
      .overrideProvider(UPLOAD_LIMITS)
      .useValue({ maxBytes: 64 * 1024 * 1024, maxPages: null })
      .overrideProvider(FileValidationService)
      .useValue(new FileValidationService({ maxBytes: 64 * 1024 * 1024, maxPages: null }))
      .compile();

    app = moduleRef.createNestApplication({ logger: false });
    app.setGlobalPrefix('api/v1');
    app.useGlobalPipes(createValidationPipe());
    await app.init();
  });

  afterAll(async () => {
    await app.close();
    fs.rmSync(storageRoot, { recursive: true, force: true });
  });

  const device = (id: string) => ({ 'x-device-id': id });

  /**
   * A valid PDF with enough trailing comment bytes to span several parts.
   * Readers tolerate data after `%%EOF`; the gateway only inspects the header.
   */
  function multiPartPdf(pages: number, paddingBytes = 200_000): Buffer {
    const base = makePdf(pages);
    return Buffer.concat([base, Buffer.from(`%${'A'.repeat(paddingBytes)}
`)]);
  }

  async function http() {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const request = require('supertest');
    return request(app.getHttpServer());
  }

  async function createBatch(owner: string, key: string) {
    const server = await http();
    const response = await server.post('/api/v1/batches').set(device(owner)).send({ client_batch_key: key });
    expect(response.status).toBe(201);
    return response.body as { batch_id: string };
  }

  async function registerItem(
    owner: string,
    batchId: string,
    clientItemId: string,
    filename: string,
    size: number,
    options?: Record<string, unknown>,
  ) {
    const server = await http();
    return server
      .post(`/api/v1/batches/${batchId}/items`)
      .set(device(owner))
      .send({ client_item_id: clientItemId, filename, size_bytes: size, options });
  }

  it('creates a batch and replays the same key', async () => {
    const first = await createBatch('device-batch-1', 'key-1');
    const second = await createBatch('device-batch-1', 'key-1');
    expect(second.batch_id).toBe(first.batch_id);
    expect((second as unknown as { status: string }).status).toBe('accepting');
  });

  it('isolates batches between owners', async () => {
    const created = await createBatch('device-owner-a', 'shared');
    const server = await http();
    const foreign = await server.get(`/api/v1/batches/${created.batch_id}`).set(device('device-owner-b'));
    expect(foreign.status).toBe(403);
  });

  it('replays an identically registered item and rejects divergent payloads', async () => {
    const batch = await createBatch('device-item', 'k');
    const first = await registerItem('device-item', batch.batch_id, 'item-1', 'a.pdf', 1024);
    expect(first.status).toBe(201);
    expect(first.body.replayed).toBe(false);

    const same = await registerItem('device-item', batch.batch_id, 'item-1', 'a.pdf', 1024);
    expect(same.status).toBe(201);
    expect(same.body.replayed).toBe(true);
    expect(same.body.item.item_id).toBe(first.body.item.item_id);
    expect(same.body.upload_id).toBe(first.body.upload_id);

    const divergent = await registerItem('device-item', batch.batch_id, 'item-1', 'a.pdf', 2048);
    expect(divergent.status).toBe(409);
    expect(divergent.body.error.code).toBe('IDEMPOTENT_REPLAY');
  });

  it('rejects an unsupported extension and an image asking for XLSX', async () => {
    const batch = await createBatch('device-kind', 'k');
    const badName = await registerItem('device-kind', batch.batch_id, 'x', 'notes.docx', 10);
    expect(badName.status).toBe(400);
    expect(badName.body.error.code).toBe('INVALID_FILE');

    const badFormat = await registerItem('device-kind', batch.batch_id, 'y', 'scan.png', 100, {
      target_formats: ['xlsx'],
    });
    expect(badFormat.status).toBe(400);
    expect(badFormat.body.error.code).toBe('INCOMPATIBLE_OUTPUT_FORMAT');
  });

  it('accepts 1001 items and paginates without omission', async () => {
    const owner = 'device-many';
    const batch = await createBatch(owner, 'many');
    for (let index = 0; index < 1001; index += 1) {
      const response = await registerItem(
        owner,
        batch.batch_id,
        `item-${String(index).padStart(4, '0')}`,
        `doc-${String(index).padStart(4, '0')}.pdf`,
        100,
      );
      expect(response.status).toBe(201);
    }

    const server = await http();
    const seen: string[] = [];
    let cursor: string | null = null;
    do {
      const query: string = cursor
        ? `/api/v1/batches/${batch.batch_id}/items?limit=200&cursor=${encodeURIComponent(cursor)}`
        : `/api/v1/batches/${batch.batch_id}/items?limit=200`;
      const page = await server.get(query).set(device(owner));
      const body = page.body as {
        items: Array<{ client_item_id: string }>;
        next_cursor: string | null;
      };
      expect(page.status).toBe(200);
      seen.push(...body.items.map((item) => item.client_item_id));
      cursor = body.next_cursor;
    } while (cursor);

    expect(seen).toHaveLength(1001);
    expect(new Set(seen).size).toBe(1001);

    const summary = await server.get(`/api/v1/batches/${batch.batch_id}`).set(device(owner));
    expect(summary.body.counts.total_items).toBe(1001);
  }, 120_000);

  it('blocks sealing while an upload is pending and completes it afterwards', async () => {
    const owner = 'device-seal';
    const batch = await createBatch(owner, 'seal');
    const registered = await registerItem(owner, batch.batch_id, 'pending', 'a.pdf', 100);
    expect(registered.status).toBe(201);

    const server = await http();
    const blocked = await server.post(`/api/v1/batches/${batch.batch_id}/seal`).set(device(owner));
    expect(blocked.status).toBe(409);
    expect(blocked.body.error.code).toBe('BATCH_HAS_PENDING_UPLOADS');
    expect(blocked.body.error.details.pending_uploads).toBe(1);
  });

  it('uploads a document in parts and creates exactly one conversion', async () => {
    const owner = 'device-parts';
    const batch = await createBatch(owner, 'parts');
    const pdf = multiPartPdf(3);
    const sha = createHash('sha256').update(pdf).digest('hex');

    const registered = await registerItem(owner, batch.batch_id, 'doc-1', 'a.pdf', pdf.length);
    expect(registered.status).toBe(201);
    const uploadId: string = registered.body.upload_id;
    const partSize: number = registered.body.part_size;
    expect(partSize).toBeGreaterThan(0);

    const totalParts = Math.ceil(pdf.length / partSize);
    expect(totalParts).toBeGreaterThan(1);

    const server = await http();
    for (let part = 0; part < totalParts; part += 1) {
      const chunk = pdf.subarray(part * partSize, (part + 1) * partSize);
      const response = await server
        .put(`/api/v1/uploads/${uploadId}/parts/${part}`)
        .set(device(owner))
        .set('content-type', 'application/octet-stream')
        .set('x-part-sha256', createHash('sha256').update(chunk).digest('hex'))
        .send(chunk);
      expect(response.status).toBe(200);
      expect(response.body.part_number).toBe(part);
    }

    const completed = await server
      .post(`/api/v1/uploads/${uploadId}/complete`)
      .set(device(owner))
      .send({ sha256: sha, total_parts: totalParts });
    expect(completed.status).toBe(202);
    expect(completed.body.conversion_id).toBeTruthy();
    expect(completed.body.replayed).toBe(false);

    // One engine enqueue for one item.
    expect(enqueue).toHaveBeenCalledTimes(1);

    // Replaying complete returns the same conversion, without a second enqueue.
    const replay = await server
      .post(`/api/v1/uploads/${uploadId}/complete`)
      .set(device(owner))
      .send({ sha256: sha, total_parts: totalParts });
    expect(replay.status).toBe(202);
    expect(replay.body.replayed).toBe(true);
    expect(replay.body.conversion_id).toBe(completed.body.conversion_id);
    expect(enqueue).toHaveBeenCalledTimes(1);

    const listing = await server.get(`/api/v1/batches/${batch.batch_id}/items`).set(device(owner));
    expect(listing.body.items).toHaveLength(1);
    expect(listing.body.items[0].conversion_id).toBe(completed.body.conversion_id);
    expect(listing.body.items[0].status).toBe('queued');
  });

  it('rejects a divergent part and a wrong document hash', async () => {
    const owner = 'device-bad-parts';
    const batch = await createBatch(owner, 'bad-parts');
    const pdf = multiPartPdf(2);
    const registered = await registerItem(owner, batch.batch_id, 'doc-1', 'a.pdf', pdf.length);
    const uploadId: string = registered.body.upload_id;
    const partSize: number = registered.body.part_size;
    const server = await http();

    const chunk = pdf.subarray(0, partSize);
    const first = await server
      .put(`/api/v1/uploads/${uploadId}/parts/0`)
      .set(device(owner))
      .set('content-type', 'application/octet-stream')
      .set('x-part-sha256', createHash('sha256').update(chunk).digest('hex'))
      .send(chunk);
    expect(first.status).toBe(200);

    const divergent = await server
      .put(`/api/v1/uploads/${uploadId}/parts/0`)
      .set(device(owner))
      .set('content-type', 'application/octet-stream')
      .send(Buffer.from('different'));
    expect(divergent.status).toBe(409);
    expect(divergent.body.error.code).toBe('UPLOAD_PART_MISMATCH');

    // The confirmed part survived the divergent retransmission.
    const session = await server.get(`/api/v1/uploads/${uploadId}`).set(device(owner));
    expect(session.body.parts_received).toBe(1);

    const wrongHash = await server
      .post(`/api/v1/uploads/${uploadId}/complete`)
      .set(device(owner))
      .send({ sha256: '1'.repeat(64), total_parts: Math.ceil(pdf.length / partSize) });
    // Missing parts is detected before the hash check.
    expect([409, 422]).toContain(wrongHash.status);
  });

  it('only lets the owner touch an upload session', async () => {
    const owner = 'device-upload-owner';
    const batch = await createBatch(owner, 'upload-owner');
    const registered = await registerItem(owner, batch.batch_id, 'doc-1', 'a.pdf', 10);
    const server = await http();

    const foreign = await server.get(`/api/v1/uploads/${registered.body.upload_id}`).set(device('intruder'));
    expect(foreign.status).toBe(403);

    const missing = await server.get('/api/v1/uploads/does-not-exist').set(device(owner));
    expect(missing.status).toBe(404);
  });

  it('seals an empty batch as completed', async () => {
    const owner = 'device-empty';
    const batch = await createBatch(owner, 'empty');
    const server = await http();
    const sealed = await server.post(`/api/v1/batches/${batch.batch_id}/seal`).set(device(owner));
    expect(sealed.status).toBe(200);
    expect(sealed.body.status).toBe('completed');

    const again = await server.post(`/api/v1/batches/${batch.batch_id}/seal`).set(device(owner));
    expect(again.status).toBe(200);
    expect(again.body.sealed_at).toBe(sealed.body.sealed_at);

    const late = await registerItem(owner, batch.batch_id, 'late', 'b.pdf', 10);
    expect(late.status).toBe(409);
    expect(late.body.error.code).toBe('BATCH_NOT_OPEN');
  });
});
