import type { INestApplication } from '@nestjs/common';
import { Test } from '@nestjs/testing';
import request from 'supertest';

import { AppModule } from '../../src/app.module';
import { PrismaService } from '../../src/database/prisma.service';
import { ConverterClient } from '../../src/modules/conversions/converter.client';
import { FakePrisma } from '../helpers/fake-prisma';

/**
 * Engine proxy through the real HTTP stack. Only the outbound `fetch` to the
 * engine is replaced, so the proxy's status, header and body handling is what
 * gets exercised.
 */

const ENGINE = 'http://engine.local';
const TOKEN = 'token-e2e-proxy';

describe('Engine proxy (e2e)', () => {
  let app: INestApplication;
  let prisma: FakePrisma;
  let fetchSpy: jest.SpyInstance;
  let owner: string;
  const deviceId = 'device-proxy-0001';

  beforeAll(async () => {
    process.env.AUTH_MODE = 'optional';
    process.env.STORAGE_BACKEND = 'local';
    process.env.CONVERTER_INTERNAL_URL = ENGINE;
    process.env.CONVERTER_INTERNAL_TOKEN = TOKEN;

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

    const response = await request(app.getHttpServer())
      .post('/api/v1/auth/anonymous')
      .send({})
      .set('x-device-id', deviceId);
    owner = (response.body.user?.id ?? response.body.user_id) as string;
    expect(owner).toBeTruthy();
  });

  beforeEach(() => {
    fetchSpy = jest.spyOn(globalThis, 'fetch');
  });

  afterEach(() => {
    fetchSpy.mockRestore();
  });

  afterAll(async () => {
    await app.close();
  });

  it('answers a deleted knowledge collection with 204 (A-11)', async () => {
    fetchSpy.mockResolvedValueOnce(new Response(null, { status: 204 }));

    await request(app.getHttpServer())
      .delete('/api/v1/knowledge/collections/col-1')
      .set('x-device-id', deviceId)
      .expect(204);

    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${ENGINE}/internal/v1/knowledge/collections/col-1`);
    expect(init.method).toBe('DELETE');
    expect((init.headers as Record<string, string>)['x-internal-token']).toBe(TOKEN);
    expect((init.headers as Record<string, string>)['x-owner-id']).toBe(owner);
  });

  it('answers a deleted chat conversation with 204 (A-11)', async () => {
    fetchSpy.mockResolvedValueOnce(new Response('', { status: 200 }));

    await request(app.getHttpServer())
      .delete('/api/v1/chat/conversations/chat-1')
      .set('x-device-id', deviceId)
      .expect(204);
  });

  it('sanitises the export filename in Content-Disposition (S-05)', async () => {
    prisma.conversions.push({
      id: 'conv-export',
      documentId: 'doc-export',
      ownerId: owner,
      activeRunId: 'run-export',
      deletedAt: null,
      createdAt: new Date(),
      updatedAt: new Date(),
    });
    fetchSpy.mockResolvedValueOnce(
      new Response(Buffer.from('conteudo'), {
        status: 200,
        headers: {
          'content-type': 'application/octet-stream',
          // Decodes to: relatório"x.xlsx (quote and non-ASCII from the engine).
          'content-disposition': "attachment; filename*=UTF-8''rel%C3%B3rio%22x.xlsx",
        },
      }),
    );

    const response = await request(app.getHttpServer())
      .get('/api/v1/conversions/conv-export/exports/exp-1/download')
      .set('x-device-id', deviceId)
      .buffer(true)
      .parse((res, callback) => {
        const chunks: Buffer[] = [];
        res.on('data', (chunk: Buffer) => chunks.push(chunk));
        res.on('end', () => callback(null, Buffer.concat(chunks)));
      })
      .expect(200);

    expect(response.headers['content-disposition']).toBe(
      `attachment; filename="rel_rio_x.xlsx"; filename*=UTF-8''rel%C3%B3rio%22x.xlsx`,
    );
    expect(response.headers['x-content-type-options']).toBe('nosniff');
  });

  it('takes the knowledge filename from the Document row (A-24)', async () => {
    prisma.documents.push({
      id: 'doc-know',
      filename: 'holerite-março.pdf',
      mimeType: 'application/pdf',
      sizeBytes: 10,
      sha256: 'abc',
      storageKey: 'aa/abc.pdf',
      ownerId: owner,
      createdAt: new Date(),
    });
    prisma.conversions.push({
      id: 'conv-know',
      documentId: 'doc-know',
      ownerId: owner,
      activeRunId: 'run-know',
      deletedAt: null,
      createdAt: new Date(),
      updatedAt: new Date(),
    });
    fetchSpy.mockResolvedValueOnce(
      new Response(JSON.stringify({ status: 'indexed', chunks: 2 }), { status: 200 }),
    );

    const response = await request(app.getHttpServer())
      .post('/api/v1/knowledge/index-jobs')
      .set('x-device-id', deviceId)
      .send({ conversion_ids: ['conv-know'] })
      .expect(202);

    expect(response.body.documents[0]).toMatchObject({ status: 'indexed', chunks: 2 });
    const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toMatchObject({
      conversion_id: 'conv-know',
      filename: 'holerite-março.pdf',
      run_id: 'run-know',
    });
  });

  it('does not reach the engine through a soft-deleted conversion (A-13)', async () => {
    prisma.conversions.push({
      id: 'conv-deleted',
      documentId: 'doc-deleted',
      ownerId: owner,
      activeRunId: 'run-deleted',
      deletedAt: new Date(),
      createdAt: new Date(),
      updatedAt: new Date(),
    });

    const response = await request(app.getHttpServer())
      .get('/api/v1/conversions/conv-deleted/text')
      .set('x-device-id', deviceId)
      .expect(404);

    expect(response.body.error.code).toBe('NOT_FOUND');
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
