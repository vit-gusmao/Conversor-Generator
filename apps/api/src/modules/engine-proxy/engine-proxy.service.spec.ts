import { EventEmitter } from 'node:events';

import type { Response as ExpressResponse } from 'express';

import { captureApiErrorAsync } from '../../../test/helpers/test-utils';
import { FakePrisma } from '../../../test/helpers/fake-prisma';
import type { AppConfigService } from '../../config/app-config.service';
import type { PrismaService } from '../../database/prisma.service';

import { EngineProxyService } from './engine-proxy.service';

const config = {
  converterInternalUrl: 'http://engine.local/',
  converterInternalToken: 'token-interno',
  converterTimeoutMs: 25,
} as unknown as AppConfigService;

/** Fetch that only settles when the caller aborts it. */
function hangUntilAborted(): (input: string, init?: RequestInit) => Promise<globalThis.Response> {
  return (_input, init) =>
    new Promise<globalThis.Response>((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => {
        const error = new Error('The operation was aborted');
        error.name = 'AbortError';
        reject(error);
      });
    });
}

/** Minimal Express-like response: enough for the stream pipe. */
function fakeResponse(): ExpressResponse & { written: string[]; ended: boolean } {
  const emitter = new EventEmitter() as EventEmitter & {
    written: string[];
    ended: boolean;
    status: jest.Mock;
    setHeader: jest.Mock;
    write: (chunk: Buffer) => boolean;
    end: () => void;
  };
  emitter.written = [];
  emitter.ended = false;
  emitter.status = jest.fn();
  emitter.setHeader = jest.fn();
  emitter.write = (chunk: Buffer) => {
    emitter.written.push(chunk.toString('utf8'));
    return true;
  };
  emitter.end = () => {
    emitter.ended = true;
    emitter.emit('finish');
  };
  return emitter as unknown as ExpressResponse & { written: string[]; ended: boolean };
}

describe('EngineProxyService', () => {
  let prisma: FakePrisma;
  let service: EngineProxyService;
  let fetchSpy: jest.SpyInstance;

  beforeEach(() => {
    prisma = new FakePrisma();
    service = new EngineProxyService(config, prisma as unknown as PrismaService);
    fetchSpy = jest.spyOn(globalThis, 'fetch');
  });

  afterEach(() => {
    fetchSpy.mockRestore();
  });

  describe('runIdFor (A-13)', () => {
    it('resolves the active run of a live conversion', async () => {
      prisma.conversions.push({ id: 'conv-live', ownerId: 'owner-1', activeRunId: 'run-2', deletedAt: null });
      prisma.runs.push({ id: 'run-1', conversionId: 'conv-live', attempt: 1 });

      await expect(service.runIdFor('owner-1', 'conv-live')).resolves.toBe('run-2');
    });

    it('treats a soft-deleted conversion as not found', async () => {
      prisma.conversions.push({
        id: 'conv-gone',
        ownerId: 'owner-1',
        activeRunId: 'run-1',
        deletedAt: new Date(),
      });

      const error = await captureApiErrorAsync(() => service.runIdFor('owner-1', 'conv-gone'));

      expect(error.status).toBe(404);
      expect(error.code).toBe('NOT_FOUND');
    });
  });

  describe('json (A-11, A-12)', () => {
    it('returns undefined on 204 without parsing a body', async () => {
      fetchSpy.mockResolvedValue(new Response(null, { status: 204 }) as globalThis.Response);

      await expect(
        service.json('/internal/v1/knowledge/collections/c1', { method: 'DELETE', ownerId: 'o1' }),
      ).resolves.toBeUndefined();
    });

    it('returns undefined on an empty 200 body', async () => {
      fetchSpy.mockResolvedValue(new Response('', { status: 200 }) as globalThis.Response);

      await expect(service.json('/internal/v1/chat/conversations/c1')).resolves.toBeUndefined();
    });

    it('parses a JSON body', async () => {
      fetchSpy.mockResolvedValue(
        new Response(JSON.stringify({ ok: true }), { status: 200 }) as globalThis.Response,
      );

      await expect(service.json<{ ok: boolean }>('/internal/v1/models/status')).resolves.toEqual({
        ok: true,
      });
    });

    it('aborts a call that does not answer within CONVERTER_TIMEOUT_MS', async () => {
      fetchSpy.mockImplementation(hangUntilAborted() as never);

      const error = await captureApiErrorAsync(() => service.json('/internal/v1/models/status'));

      expect(error.status).toBe(503);
      expect(error.details).toMatchObject({ reason: 'timeout', timeout_ms: 25 });
    });

    it('keeps the deadline armed while the body is read', async () => {
      // Headers arrive at once; the body only errors when the deadline aborts it.
      fetchSpy.mockImplementation(((_input: string, init?: RequestInit) => {
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            init?.signal?.addEventListener('abort', () => {
              const error = new Error('The operation was aborted');
              error.name = 'AbortError';
              controller.error(error);
            });
          },
        });
        return Promise.resolve(new Response(body, { status: 200 }));
      }) as never);

      const error = await captureApiErrorAsync(() => service.json('/internal/v1/models/status'));

      expect(error.status).toBe(503);
      expect(error.details).toMatchObject({ reason: 'timeout' });
    });
  });

  describe('binary (A-12)', () => {
    it('aborts a download that stalls past CONVERTER_TIMEOUT_MS', async () => {
      fetchSpy.mockImplementation(hangUntilAborted() as never);

      const error = await captureApiErrorAsync(() =>
        service.binary('/internal/v1/runs/r1/exports/e1/download'),
      );

      expect(error.status).toBe(503);
      expect(error.details).toMatchObject({ reason: 'timeout' });
    });
  });

  describe('stream (A-12)', () => {
    it('fails with 503 when the engine does not send headers within the deadline', async () => {
      fetchSpy.mockImplementation(hangUntilAborted() as never);
      const response = fakeResponse();

      const error = await captureApiErrorAsync(() =>
        service.stream(response, '/internal/v1/chat/conversations/c1/messages', {
          body: { content: 'oi' },
          ownerId: 'o1',
        }),
      );

      expect(error.status).toBe(503);
      expect(error.details).toMatchObject({ reason: 'timeout' });
    });

    it('does not cut a long-lived stream once its headers have arrived', async () => {
      const encoder = new TextEncoder();
      const body = new ReadableStream<Uint8Array>({
        async start(controller) {
          controller.enqueue(encoder.encode('data: one\n\n'));
          // Longer than the 25 ms deadline: must not abort the open stream.
          await new Promise((resolve) => setTimeout(resolve, 60));
          controller.enqueue(encoder.encode('data: two\n\n'));
          controller.close();
        },
      });
      fetchSpy.mockResolvedValue(
        new Response(body, {
          status: 200,
          headers: { 'content-type': 'text/event-stream' },
        }) as globalThis.Response,
      );
      const response = fakeResponse();

      await service.stream(response, '/internal/v1/chat/conversations/c1/messages', {
        body: { content: 'oi' },
        ownerId: 'o1',
      });

      expect(response.written.join('')).toBe('data: one\n\ndata: two\n\n');
      expect(response.ended).toBe(true);
    });
  });
});
