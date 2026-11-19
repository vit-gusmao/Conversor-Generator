import http from 'node:http';

import type { INestApplication } from '@nestjs/common';
import { Test } from '@nestjs/testing';
import request from 'supertest';

import { AppModule } from '../../src/app.module';
import { PrismaService } from '../../src/database/prisma.service';
import { ConversionEventsService } from '../../src/modules/realtime/conversion-events.service';
import { ConverterClient } from '../../src/modules/conversions/converter.client';
import { FakePrisma } from '../helpers/fake-prisma';

/**
 * F1 e2e: a retry reuses the conversion id with a new run. The SSE stream of
 * that conversion must not end on the previous run's failure.
 */

interface SseClient {
  status: number;
  text: () => string;
  ended: Promise<void>;
  isEnded: () => boolean;
  close: () => void;
}

function openSse(port: number, path: string, headers: Record<string, string>): Promise<SseClient> {
  return new Promise<SseClient>((resolve, reject) => {
    let data = '';
    let ended = false;
    let markEnded: () => void = () => undefined;
    const endedPromise = new Promise<void>((done) => {
      markEnded = done;
    });
    const req = http.get({ host: '127.0.0.1', port, path, headers }, (res) => {
      res.setEncoding('utf8');
      res.on('data', (chunk: string) => {
        data += chunk;
      });
      const finish = (): void => {
        ended = true;
        markEnded();
      };
      res.on('end', finish);
      res.on('close', finish);
      resolve({
        status: res.statusCode ?? 0,
        text: () => data,
        ended: endedPromise,
        isEnded: () => ended,
        close: () => req.destroy(),
      });
    });
    req.on('error', reject);
  });
}

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

describe('Conversion events SSE (e2e, F1 / A-23)', () => {
  let app: INestApplication;
  let prisma: FakePrisma;
  let events: ConversionEventsService;
  let port: number;
  let owner: string;
  const deviceId = 'device-sse-0001';
  const clients: SseClient[] = [];

  beforeAll(async () => {
    process.env.AUTH_MODE = 'optional';
    process.env.STORAGE_BACKEND = 'local';

    prisma = new FakePrisma();
    const converter = {
      enqueue: jest.fn(),
      status: jest.fn(),
      cancel: jest.fn(),
      health: jest.fn(),
      safeHealth: jest.fn(),
    };

    const moduleRef = await Test.createTestingModule({ imports: [AppModule] })
      .overrideProvider(PrismaService)
      .useValue(prisma)
      .overrideProvider(ConverterClient)
      .useValue(converter)
      .compile();

    app = moduleRef.createNestApplication({ logger: false });
    app.setGlobalPrefix('api/v1');
    await app.init();
    await app.listen(0, '127.0.0.1');
    const address = app.getHttpServer().address() as { port: number };
    port = address.port;
    events = app.get(ConversionEventsService);

    const response = await request(app.getHttpServer())
      .post('/api/v1/auth/anonymous')
      .send({})
      .set('x-device-id', deviceId);
    owner = (response.body.user?.id ?? response.body.user_id) as string;
    expect(owner).toBeTruthy();
  });

  afterEach(() => {
    for (const client of clients.splice(0)) client.close();
  });

  afterAll(async () => {
    await app.close();
  });

  /** Conversion whose first run failed and whose retry (run-2) is in progress. */
  async function seedRetriedConversion(conversionId: string): Promise<void> {
    prisma.conversions.push({
      id: conversionId,
      documentId: `doc-${conversionId}`,
      ownerId: owner,
      status: 'queued',
      activeRunId: `${conversionId}-run-2`,
      deletedAt: null,
      createdAt: new Date(),
      updatedAt: new Date(),
    });
    prisma.runs.push(
      { id: `${conversionId}-run-1`, conversionId, attempt: 1, status: 'failed', createdAt: new Date() },
      { id: `${conversionId}-run-2`, conversionId, attempt: 2, status: 'queued', createdAt: new Date() },
    );
    await events.record(conversionId, {
      type: 'status',
      status: 'queued',
      stage: 'receive',
      progress: 0.05,
      message: 'primeira tentativa',
      runId: `${conversionId}-run-1`,
    });
    await events.record(conversionId, {
      type: 'failed',
      status: 'failed',
      stage: 'failed',
      progress: 1,
      message: 'falhou na primeira tentativa',
      runId: `${conversionId}-run-1`,
    });
    await events.record(conversionId, {
      type: 'status',
      status: 'queued',
      stage: 'inspect',
      progress: 0.05,
      message: 'nova tentativa',
      runId: `${conversionId}-run-2`,
    });
  }

  it('does not end on a previous run failure after a retry (F1)', async () => {
    await seedRetriedConversion('conv-retry');

    const sse = await openSse(port, '/api/v1/conversions/conv-retry/events', {
      'x-device-id': deviceId,
    });
    clients.push(sse);
    await sleep(150);

    expect(sse.status).toBe(200);
    // The old failure is replayed, the stream stays open for the retry.
    expect(sse.text()).toContain('id: 2\nevent: failed');
    expect(sse.text()).toContain('id: 3\nevent: status');
    expect(sse.isEnded()).toBe(false);

    await events.record('conv-retry', {
      type: 'completed',
      status: 'completed',
      stage: 'done',
      progress: 1,
      message: 'concluída na segunda tentativa',
      runId: 'conv-retry-run-2',
    });

    await sse.ended;
    expect(sse.text()).toContain('id: 4\nevent: completed');
  });

  it('follows one run with ?run_id= and ends on that run terminal event', async () => {
    await seedRetriedConversion('conv-run-filter');

    const sse = await openSse(
      port,
      '/api/v1/conversions/conv-run-filter/events?run_id=conv-run-filter-run-1',
      { 'x-device-id': deviceId },
    );
    clients.push(sse);

    await sse.ended;
    expect(sse.text()).toContain('id: 1\nevent: status');
    expect(sse.text()).toContain('id: 2\nevent: failed');
    expect(sse.text()).not.toContain('nova tentativa');
  });

  it('still ends on the terminal event of a single-run conversion', async () => {
    prisma.conversions.push({
      id: 'conv-single',
      documentId: 'doc-conv-single',
      ownerId: owner,
      status: 'failed',
      activeRunId: 'conv-single-run-1',
      deletedAt: null,
      createdAt: new Date(),
      updatedAt: new Date(),
    });
    prisma.runs.push({ id: 'conv-single-run-1', conversionId: 'conv-single', attempt: 1, status: 'failed' });
    await events.record('conv-single', {
      type: 'failed',
      status: 'failed',
      stage: 'failed',
      progress: 1,
      message: 'falha única',
      runId: 'conv-single-run-1',
    });

    const sse = await openSse(port, '/api/v1/conversions/conv-single/events', {
      'x-device-id': deviceId,
    });
    clients.push(sse);

    await sse.ended;
    expect(sse.text()).toContain('falha única');
  });

  it('rejects a malformed run_id with 400', async () => {
    await seedRetriedConversion('conv-bad-run');

    const response = await request(app.getHttpServer())
      .get('/api/v1/conversions/conv-bad-run/events?run_id=..%2Fetc')
      .set('x-device-id', deviceId)
      .expect(400);

    expect(response.body.error.code).toBe('VALIDATION_ERROR');
  });

  it('returns 404 for a run that does not belong to the conversion', async () => {
    await seedRetriedConversion('conv-foreign-run');

    const response = await request(app.getHttpServer())
      .get('/api/v1/conversions/conv-foreign-run/events?run_id=conv-retry-run-1')
      .set('x-device-id', deviceId)
      .expect(404);

    expect(response.body.error.code).toBe('NOT_FOUND');
  });
});
