import type { INestApplication } from '@nestjs/common';
import { Test } from '@nestjs/testing';
import request from 'supertest';

import { AppModule } from '../../src/app.module';
import { PrismaService } from '../../src/database/prisma.service';
import { ConverterClient } from '../../src/modules/conversions/converter.client';
import { FakePrisma } from '../helpers/fake-prisma';

/**
 * Client-side body errors must answer with the public error envelope and a
 * client status, never a 500 (S-01).
 */
describe('HTTP body errors (e2e, S-01)', () => {
  let app: INestApplication;

  beforeAll(async () => {
    process.env.AUTH_MODE = 'optional';
    process.env.STORAGE_BACKEND = 'local';

    const prisma = new FakePrisma();
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
  });

  afterAll(async () => {
    await app.close();
  });

  it('answers malformed JSON with 400 and the error envelope', async () => {
    const response = await request(app.getHttpServer())
      .post('/api/v1/knowledge/collections')
      .set('x-device-id', 'device-body-0001')
      .set('content-type', 'application/json')
      .send('{"name": ');

    expect(response.status).toBe(400);
    expect(response.body.error).toMatchObject({ code: 'VALIDATION_ERROR' });
  });

  it('answers a body over the limit with 413', async () => {
    const oversized = JSON.stringify({ blob: 'x'.repeat(300 * 1024) });

    const response = await request(app.getHttpServer())
      .post('/api/v1/knowledge/collections')
      .set('x-device-id', 'device-body-0002')
      .set('content-type', 'application/json')
      .send(oversized);

    expect(response.status).toBe(413);
    expect(response.body.error).toMatchObject({ code: 'FILE_TOO_LARGE' });
  });
});
