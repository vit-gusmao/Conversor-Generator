import * as os from 'node:os';
import * as path from 'node:path';

import type { INestApplication } from '@nestjs/common';
import { Test } from '@nestjs/testing';
import request from 'supertest';

import { AppModule } from '../../src/app.module';
import { createValidationPipe } from '../../src/common/pipes/validation.pipe';
import { PrismaService } from '../../src/database/prisma.service';
import { ConverterClient } from '../../src/modules/conversions/converter.client';
import { FileValidationService } from '../../src/upload/file-validation.service';
import { STORAGE_SERVICE, type StorageService } from '../../src/upload/storage.service';
import { FakePrisma } from '../helpers/fake-prisma';
import { makePdf, uuidPattern } from '../helpers/test-utils';

const converterHealth = {
  status: 'ok',
  engine_version: '1.0.0',
  ocr_enabled: true,
  ocr_engine: 'tesseract',
  queue: 'inline',
  capabilities: { ocr: true, tables: true },
};

describe('API (e2e)', () => {
  let app: INestApplication;
  let prisma: FakePrisma;
  let converter: {
    enqueue: jest.Mock;
    status: jest.Mock;
    cancel: jest.Mock;
    health: jest.Mock;
    safeHealth: jest.Mock;
    result: jest.Mock;
    preview: jest.Mock;
    download: jest.Mock;
    auditSummary: jest.Mock;
  };
  let storage: StorageService;

  const deviceId = 'device-e2e-0001';
  const otherDeviceId = 'device-e2e-0002';

  beforeAll(async () => {
    process.env.AUTH_MODE = 'optional';
    process.env.STORAGE_BACKEND = 'local';
    process.env.STORAGE_LOCAL_ROOT = path.join(os.tmpdir(), `pdf2x-api-e2e-${process.pid}`);

    prisma = new FakePrisma();
    converter = {
      enqueue: jest.fn().mockResolvedValue({ accepted: true, run_id: 'run', status: 'queued' }),
      status: jest.fn().mockResolvedValue({
        run_id: 'run',
        status: 'queued',
        progress: 0.05,
        stage: 'inspect',
        events: [],
      }),
      cancel: jest.fn().mockResolvedValue(undefined),
      health: jest.fn().mockResolvedValue(converterHealth),
      safeHealth: jest.fn().mockResolvedValue(converterHealth),
      result: jest.fn(),
      preview: jest.fn(),
      download: jest.fn(),
      auditSummary: jest.fn(),
    };
    storage = {
      backend: 'local',
      put: jest.fn().mockResolvedValue({ key: 'aa/hash.pdf', size: 1 }),
      get: jest.fn(),
      delete: jest.fn().mockResolvedValue(undefined),
      exists: jest.fn().mockResolvedValue(true),
    } as unknown as StorageService;

    const moduleRef = await Test.createTestingModule({ imports: [AppModule] })
      .overrideProvider(PrismaService)
      .useValue(prisma)
      .overrideProvider(ConverterClient)
      .useValue(converter)
      .overrideProvider(STORAGE_SERVICE)
      .useValue(storage)
      .overrideProvider(FileValidationService)
      .useValue(new FileValidationService({ maxBytes: 10 * 1024 * 1024, maxPages: 500 }))
      .compile();

    app = moduleRef.createNestApplication({ logger: false });
    app.setGlobalPrefix('api/v1');
    app.useGlobalPipes(createValidationPipe());
    await app.init();
  });

  afterAll(async () => {
    await app.close();
  });

  it('GET /api/v1/settings returns PlatformSettings', async () => {
    const response = await request(app.getHttpServer()).get('/api/v1/settings').expect(200);

    expect(response.body.auth_mode).toBe('optional');
    expect(response.body.max_upload_bytes).toBeGreaterThan(0);
    expect(response.body.max_pages).toBeGreaterThan(0);
    expect(response.body.storage_backend).toBe('local');
    expect(response.body.capabilities.converter).toBe(true);
    expect(response.body.retention).toMatchObject({
      pdf_days: expect.any(Number),
      xlsx_days: expect.any(Number),
    });
  });

  it('GET /api/v1/health reports every service', async () => {
    const response = await request(app.getHttpServer()).get('/api/v1/health').expect(200);

    expect(response.body.services).toMatchObject({
      db: 'up',
      storage: 'up',
      converter: 'up',
    });
    expect(['ok', 'degraded']).toContain(response.body.status);
  });

  it('POST /api/v1/conversions accepts a PDF and returns 202', async () => {
    const response = await request(app.getHttpServer())
      .post('/api/v1/conversions')
      .set('x-device-id', deviceId)
      .attach('file', makePdf(2), 'holerite.pdf')
      .expect(202);

    expect(response.body.conversion_id).toMatch(uuidPattern);
    expect(response.body.status).toBe('queued');
    expect(response.body.message).toBe('Conversão recebida e enfileirada.');
    expect(response.body.links.events).toBe(
      `/api/v1/conversions/${response.body.conversion_id}/events`,
    );
  });

  it('rejects an upload without a file', async () => {
    const response = await request(app.getHttpServer())
      .post('/api/v1/conversions')
      .set('x-device-id', deviceId)
      .expect(400);

    expect(response.body.error.code).toBe('INVALID_FILE');
  });

  it('exposes the conversion only to the device that created it', async () => {
    const created = await request(app.getHttpServer())
      .post('/api/v1/conversions')
      .set('x-device-id', deviceId)
      .attach('file', makePdf(3), 'contracheque.pdf')
      .expect(202);

    const id = created.body.conversion_id as string;

    const mine = await request(app.getHttpServer())
      .get(`/api/v1/conversions/${id}`)
      .set('x-device-id', deviceId)
      .expect(200);
    expect(mine.body.document.filename).toBe('contracheque.pdf');
    expect(mine.body.status).toBe('queued');
    expect(mine.body.progress).toBeGreaterThanOrEqual(0);

    const foreign = await request(app.getHttpServer())
      .get(`/api/v1/conversions/${id}`)
      .set('x-device-id', otherDeviceId)
      .expect(403);
    expect(foreign.body.error.code).toBe('FORBIDDEN');
  });

  it('lists conversations scoped to the caller', async () => {
    const response = await request(app.getHttpServer())
      .get('/api/v1/conversions')
      .set('x-device-id', deviceId)
      .expect(200);

    expect(response.body.page).toBe(1);
    expect(response.body.total).toBeGreaterThanOrEqual(2);
    expect(Array.isArray(response.body.items)).toBe(true);
    expect(response.body.items[0]).toHaveProperty('filename');
  });

  it('rejects invalid pagination with a pt-BR validation error', async () => {
    const response = await request(app.getHttpServer())
      .get('/api/v1/conversions?page_size=5000')
      .set('x-device-id', deviceId)
      .expect(422);

    expect(response.body.error.code).toBe('VALIDATION_ERROR');
    expect(response.body.error.details.errors.join(' ')).toContain('page_size');
  });

  it('protects the internal hook with the shared token', async () => {
    await request(app.getHttpServer())
      .post('/api/v1/internal/hooks/converter')
      .send({ conversion_id: 'x', status: 'extracting', progress: 0.3 })
      .expect(401);

    await request(app.getHttpServer())
      .post('/api/v1/internal/hooks/converter')
      .set('x-internal-token', process.env.CONVERTER_INTERNAL_TOKEN ?? 'dev-internal-token-change-me')
      .send({ status: 'extracting', progress: 0.3 })
      .expect(404);
  });
});
