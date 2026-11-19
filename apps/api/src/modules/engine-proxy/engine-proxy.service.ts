import { Injectable, Logger } from '@nestjs/common';
import type { Response } from 'express';

import { ApiError } from '../../common/errors/api-error';
import type { ErrorCode } from '../../common/errors/error-codes';
import { AppConfigService } from '../../config/app-config.service';
import { PrismaService } from '../../database/prisma.service';

/** Forwards requests that live in the engine's internal API. */
@Injectable()
export class EngineProxyService {
  private readonly logger = new Logger(EngineProxyService.name);

  constructor(
    private readonly config: AppConfigService,
    private readonly prisma: PrismaService,
  ) {}

  private get base(): string {
    return this.config.converterInternalUrl.replace(/\/+$/, '');
  }

  private get headers(): Record<string, string> {
    return {
      'x-internal-token': this.config.converterInternalToken,
      'Content-Type': 'application/json',
      Accept: 'application/json',
    };
  }

  async runIdFor(userId: string, conversionId: string): Promise<string> {
    const row = await this.prisma.conversion.findFirst({
      where: { id: conversionId, ownerId: userId },
      select: {
        activeRunId: true,
        runs: { orderBy: { attempt: 'desc' }, take: 1, select: { id: true } },
      },
    });
    if (!row) throw ApiError.notFound({ resource: 'conversion' });
    const runId = row.activeRunId ?? row.runs[0]?.id;
    if (!runId) throw ApiError.conflict('NOT_READY' as ErrorCode, { resource: 'conversion_run' });
    return runId;
  }

  async json<T>(
    path: string,
    options: {
      method?: string;
      body?: unknown;
      ownerId?: string;
      headers?: Record<string, string>;
    } = {},
  ): Promise<T> {
    let response: globalThis.Response;
    try {
      response = await fetch(`${this.base}${path}`, {
        method: options.method ?? 'GET',
        headers: {
          ...this.headers,
          ...(options.ownerId ? { 'x-owner-id': options.ownerId } : {}),
          ...(options.headers ?? {}),
        },
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
      });
    } catch (error) {
      throw ApiError.converterUnavailable({ cause: String(error) });
    }
    if (!response.ok) {
      throw await this.toError(response);
    }
    return (await response.json()) as T;
  }

  async binary(
    path: string,
    ownerId?: string,
  ): Promise<{ body: Buffer; contentType: string; filename: string | null; status: number }> {
    let response: globalThis.Response;
    try {
      response = await fetch(`${this.base}${path}`, {
        headers: {
          ...this.headers,
          ...(ownerId ? { 'x-owner-id': ownerId } : {}),
        },
      });
    } catch (error) {
      throw ApiError.converterUnavailable({ cause: String(error) });
    }
    if (!response.ok) throw await this.toError(response);
    const buffer = Buffer.from(await response.arrayBuffer());
    return {
      body: buffer,
      contentType: response.headers.get('content-type') ?? 'application/octet-stream',
      filename: parseFilename(response.headers.get('content-disposition')),
      status: response.status,
    };
  }

  /** Streams the engine's SSE response to the client without buffering. */
  async stream(
    response: Response,
    path: string,
    options: { method?: string; body?: unknown; ownerId: string },
  ): Promise<void> {
    let upstream: globalThis.Response;
    try {
      upstream = await fetch(`${this.base}${path}`, {
        method: options.method ?? 'POST',
        headers: {
          ...this.headers,
          Accept: 'text/event-stream',
          'x-owner-id': options.ownerId,
        },
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
      });
    } catch (error) {
      throw ApiError.converterUnavailable({ cause: String(error) });
    }
    if (!upstream.ok || !upstream.body) throw await this.toError(upstream);

    response.status(upstream.status);
    response.setHeader('Content-Type', 'text/event-stream; charset=utf-8');
    response.setHeader('Cache-Control', 'no-cache, no-transform');
    response.setHeader('Connection', 'keep-alive');
    response.setHeader('X-Accel-Buffering', 'no');

    const reader = upstream.body.getReader();
    const abort = (): void => {
      void reader.cancel().catch(() => undefined);
    };
    response.on('close', abort);
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        response.write(Buffer.from(value));
      }
    } finally {
      response.off('close', abort);
      response.end();
    }
  }

  private async toError(response: globalThis.Response): Promise<ApiError> {
    let code = `HTTP_${response.status}`;
    let message = 'A operação no motor de conversão falhou.';
    let details: Record<string, unknown> | undefined;
    try {
      const payload = (await response.json()) as {
        error?: { code?: string; message?: string; details?: Record<string, unknown> };
      };
      if (payload.error?.code) code = payload.error.code;
      if (payload.error?.message) message = payload.error.message;
      details = payload.error?.details;
    } catch {
      // keep the generic message
    }
    this.logger.warn(`engine proxy ${response.status} ${code}`);
    return new ApiError(code as ErrorCode, response.status, message, details);
  }
}

function parseFilename(disposition: string | null): string | null {
  if (!disposition) return null;
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
  if (encoded?.[1]) return decodeURIComponent(encoded[1]);
  const plain = /filename="([^"]+)"/i.exec(disposition);
  return plain?.[1] ?? null;
}
