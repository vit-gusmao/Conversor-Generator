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

  private get timeoutMs(): number {
    return this.config.converterTimeoutMs;
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
      // A soft-deleted conversion is gone for its owner: no run may be reached through it.
      where: { id: conversionId, ownerId: userId, deletedAt: null },
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

  /**
   * JSON call bounded by `CONVERTER_TIMEOUT_MS`. The deadline covers the body
   * read too. A 204 or an empty body yields `undefined` (DELETE routes).
   */
  async json<T>(
    path: string,
    options: {
      method?: string;
      body?: unknown;
      ownerId?: string;
      headers?: Record<string, string>;
    } = {},
  ): Promise<T> {
    return this.withDeadline(async (signal) => {
      const response = await fetch(`${this.base}${path}`, {
        method: options.method ?? 'GET',
        headers: {
          ...this.headers,
          ...(options.ownerId ? { 'x-owner-id': options.ownerId } : {}),
          ...(options.headers ?? {}),
        },
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        signal,
      });
      if (!response.ok) {
        throw await this.toError(response);
      }
      if (response.status === 204) return undefined as T;
      const text = await response.text();
      if (!text) return undefined as T;
      try {
        return JSON.parse(text) as T;
      } catch {
        throw ApiError.converterUnavailable({
          reason: 'invalid_json_from_engine',
          status: response.status,
        });
      }
    });
  }

  async binary(
    path: string,
    ownerId?: string,
  ): Promise<{ body: Buffer; contentType: string; filename: string | null; status: number }> {
    return this.withDeadline(async (signal) => {
      const response = await fetch(`${this.base}${path}`, {
        headers: {
          ...this.headers,
          ...(ownerId ? { 'x-owner-id': ownerId } : {}),
        },
        signal,
      });
      if (!response.ok) throw await this.toError(response);
      const buffer = Buffer.from(await response.arrayBuffer());
      return {
        body: buffer,
        contentType: response.headers.get('content-type') ?? 'application/octet-stream',
        filename: parseFilename(response.headers.get('content-disposition')),
        status: response.status,
      };
    });
  }

  /**
   * Streams the engine's SSE response to the client without buffering.
   *
   * Time to first byte is bounded by `CONVERTER_TIMEOUT_MS`: the engine must
   * answer its headers within that window. Once the stream is open it is
   * long-lived, so only a client disconnect or the engine closing it ends it.
   */
  async stream(
    response: Response,
    path: string,
    options: { method?: string; body?: unknown; ownerId: string },
  ): Promise<void> {
    const controller = new AbortController();
    const ttfb = setTimeout(() => controller.abort(), this.timeoutMs);
    const onClientClose = (): void => controller.abort();
    response.on('close', onClientClose);

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
        signal: controller.signal,
      });
    } catch (error) {
      response.off('close', onClientClose);
      throw this.transportError(error);
    } finally {
      clearTimeout(ttfb);
    }
    if (!upstream.ok || !upstream.body) {
      response.off('close', onClientClose);
      throw await this.toError(upstream);
    }

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
    } catch (error) {
      // Client gone or upstream dropped mid-stream: close our side cleanly.
      this.logger.debug(`stream do motor encerrado: ${String(error)}`);
    } finally {
      response.off('close', abort);
      response.off('close', onClientClose);
      response.end();
    }
  }

  /** Runs one engine call under the converter deadline. */
  private async withDeadline<T>(run: (signal: AbortSignal) => Promise<T>): Promise<T> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      return await run(controller.signal);
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw this.transportError(error);
    } finally {
      clearTimeout(timer);
    }
  }

  private transportError(error: unknown): ApiError {
    if (isAbortError(error)) {
      return ApiError.converterUnavailable({ reason: 'timeout', timeout_ms: this.timeoutMs });
    }
    return ApiError.converterUnavailable({ cause: String(error) });
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

function isAbortError(error: unknown): boolean {
  return typeof error === 'object' && error !== null && (error as { name?: unknown }).name === 'AbortError';
}

function parseFilename(disposition: string | null): string | null {
  if (!disposition) return null;
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
  if (encoded?.[1]) {
    try {
      return decodeURIComponent(encoded[1]);
    } catch {
      return encoded[1];
    }
  }
  const plain = /filename="([^"]+)"/i.exec(disposition);
  return plain?.[1] ?? null;
}
