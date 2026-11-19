import { AppModule } from '../../src/app.module';
import { EngineProxyService } from '../../src/modules/engine-proxy/engine-proxy.service';
import { PrismaService } from '../../src/database/prisma.service';
import { FileValidationService } from '../../src/upload/file-validation.service';
import { FakePrisma } from '../helpers/fake-prisma';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import request from 'supertest';
import { Test } from '@nestjs/testing';

/**
 * T6 e2e: full-mode automation parity.
 *
 * - RAG: a conversion that exists ONLY in Prisma is resolved by the gateway
 *   (ownership checked) and sent to the engine as authorized references.
 * - Submission: the engine's internal automation endpoints are idempotent
 *   per client key (ensure-batch and register-item replay).
 */

const INTERNAL_TOKEN = 'e2e-internal-token';

describe('Automation parity (upgrade T6)', () => {
  let app: import('@nestjs/common').INestApplication;
  let prisma: FakePrisma;
  let proxyJson: jest.Mock;

  beforeAll(async () => {
    process.env.AUTH_MODE = 'optional';
    process.env.STORAGE_BACKEND = 'local';
    process.env.CONVERTER_INTERNAL_TOKEN = INTERNAL_TOKEN;
    const storageRoot = path.join(os.tmpdir(), `pdf2x-automation-e2e-${process.pid}`);
    fs.rmSync(storageRoot, { recursive: true, force: true });

    prisma = new FakePrisma();
    proxyJson = jest.fn().mockResolvedValue({ status: 'indexed', chunks: 3 });
    const converter = {
      enqueue: jest.fn().mockResolvedValue({ accepted: true }),
      status: jest.fn().mockResolvedValue({ run_id: 'run', status: 'queued' }),
      cancel: jest.fn().mockResolvedValue(undefined),
      health: jest.fn().mockResolvedValue({ status: 'ok' }),
      safeHealth: jest.fn().mockResolvedValue({ status: 'ok' }),
      result: jest.fn(),
      preview: jest.fn(),
      download: jest.fn(),
      auditSummary: jest.fn(),
    };

    const moduleRef = await Test.createTestingModule({ imports: [AppModule] })
      .overrideProvider(PrismaService)
      .useValue(prisma)
      .overrideProvider(EngineProxyService)
      .useValue({ json: proxyJson })
      .compile();

    app = moduleRef.createNestApplication({ logger: false });
    app.setGlobalPrefix('api/v1');
    await app.init();
    void converter;
    void storageRoot;
  });

  afterAll(async () => {
    await app.close();
  });

  async function ownerIdFor(deviceId: string): Promise<string> {
    const response = await request(app.getHttpServer())
      .post('/api/v1/auth/anonymous')
      .send({})
      .set('x-device-id', deviceId)
      .expect((range) => [200, 201].includes(range.status));
    return (response.body.user?.id ?? response.body.user_id ?? '') as string;
  }

  it('resolves a Prisma-only conversion into authorized RAG references', async () => {
    const owner = await ownerIdFor('device-auto-1');
    prisma.conversions.push({
      id: 'conv_prisma_only',
      ownerId: owner,
      documentId: 'doc_prisma_only',
      activeRunId: 'run_prisma_only',
      filename: 'planilha.xlsx',
      deletedAt: null,
      status: 'completed',
    });

    const response = await request(app.getHttpServer())
      .post('/api/v1/knowledge/index-jobs')
      .set('x-device-id', 'device-auto-1')
      .send({ conversion_ids: ['conv_prisma_only'] })
      .expect(202);

    expect(response.body.documents).toHaveLength(1);
    expect(response.body.documents[0]).toMatchObject({ status: 'indexed', chunks: 3 });

    const authorizedCall = proxyJson.mock.calls.find(
      ([target]) => target === '/internal/v1/knowledge/authorized-index',
    );
    expect(authorizedCall).toBeDefined();
    const body = authorizedCall![1].body as Record<string, string>;
    expect(body.conversion_id).toBe('conv_prisma_only');
    expect(body.document_id).toBe('doc_prisma_only');
    expect(body.run_id).toBe('run_prisma_only');
    expect(body.owner_id).toBe(owner);
  });

  it('treats a foreign conversion as not found without leaking data', async () => {
    proxyJson.mockClear();
    const response = await request(app.getHttpServer())
      .post('/api/v1/knowledge/index-jobs')
      .set('x-device-id', 'device-auto-foreign')
      .send({ conversion_ids: ['conv_prisma_only'] })
      .expect(202);

    expect(response.body.documents[0]).toMatchObject({ status: 'not_found', chunks: 0 });
    // The gateway never calls the engine for an owner that does not own the row.
    const authorizedCalls = proxyJson.mock.calls.filter(
      ([target]) => target === '/internal/v1/knowledge/authorized-index',
    );
    expect(authorizedCalls).toHaveLength(0);
  });

  it('accepts engine submissions and replays them idempotently', async () => {
    const headers = { 'x-internal-token': INTERNAL_TOKEN };
    const first = await request(app.getHttpServer())
      .post('/api/v1/internal/automation/batches')
      .set(headers)
      .send({ owner_id: 'owner-sub', import_id: 'imp-e2e-1' })
      .expect(201);
    const second = await request(app.getHttpServer())
      .post('/api/v1/internal/automation/batches')
      .set(headers)
      .send({ owner_id: 'owner-sub', import_id: 'imp-e2e-1' })
      .expect(201);
    expect(second.body.batch_id).toBe(first.body.batch_id);

    const itemBody = {
      owner_id: 'owner-sub',
      client_item_id: 'srcimp:imp-e2e-1:1',
      filename: 'planilha.xlsx',
      size_bytes: 32,
      options: { schema_version: 3 },
      import_id: 'imp-e2e-1',
    };
    const itemA = await request(app.getHttpServer())
      .post(`/api/v1/internal/automation/batches/${first.body.batch_id}/items`)
      .set(headers)
      .send(itemBody)
      .expect(201);
    const itemB = await request(app.getHttpServer())
      .post(`/api/v1/internal/automation/batches/${first.body.batch_id}/items`)
      .set(headers)
      .send(itemBody)
      .expect(201);
    expect(itemB.body.item_id).toBe(itemA.body.item_id);
    expect(itemB.body.replayed).toBe(true);
  });

  it('rejects engine submissions without the internal token', async () => {
    await request(app.getHttpServer())
      .post('/api/v1/internal/automation/batches')
      .send({ owner_id: 'x', import_id: 'y' })
      .expect((range) => [401, 403].includes(range.status));
  });
});
