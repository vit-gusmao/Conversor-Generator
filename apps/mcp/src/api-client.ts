/**
 * HTTP client for the Conversor Generator public API (`/api/v1`).
 *
 * Rules this file enforces:
 *  - the owner never travels in a body: only the credential header does;
 *  - the error envelope `{error:{code,message,details}}` becomes a typed error;
 *  - only network failures, 429 and 503 are retried, with exponential backoff
 *    and one stable idempotency key per logical operation;
 *  - uploads and downloads stream; no method buffers a whole document.
 */

import { createHash } from 'node:crypto';
import { Readable } from 'node:stream';

import type {
  BatchItem,
  BatchSummary,
  CompleteUploadResponse,
  CursorPage,
  ExportList,
  Preview,
  RegisterBatchItemRequest,
  RegisterBatchItemResponse,
  ResultSummary,
  RetryItemResponse,
  RetryScope,
  SealBatchResponse,
  UploadPartReceipt,
  UploadSessionDetail,
} from './types.js';

export class ApiClientError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.name = 'ApiClientError';
    this.status = status;
    this.code = code;
    this.details = details;
  }

  get retryable(): boolean {
    return this.status === 429 || this.status === 503;
  }
}

export interface ApiClientOptions {
  baseUrl: string;
  deviceId: string;
  fetchImpl?: typeof fetch;
  requestTimeoutMs?: number;
  maxAttempts?: number;
  retryBaseDelayMs?: number;
  sleep?: (ms: number) => Promise<void>;
  log?: (message: string) => void;
}

interface RequestOptions {
  body?: unknown;
  query?: Record<string, string | number | null | undefined>;
  idempotencyKey?: string;
  timeoutMs?: number;
  /** Set to 1 for bodies that cannot be replayed (streams). */
  maxAttempts?: number;
}

type HttpMethod = 'GET' | 'POST' | 'PUT' | 'DELETE';

const DEFAULT_TIMEOUT_MS = 60_000;
const DEFAULT_MAX_ATTEMPTS = 3;
const DEFAULT_RETRY_BASE_MS = 250;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function parseFilename(header: string | null, fallback: string): string {
  if (!header) return fallback;
  const extended = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(header);
  if (extended?.[1]) {
    try {
      return decodeURIComponent(extended[1].trim());
    } catch {
      /* fall through to the plain form */
    }
  }
  const plain = /filename\s*=\s*"?([^";]+)"?/i.exec(header);
  if (plain?.[1]) return plain[1].trim();
  return fallback;
}

/** Stable key for a part upload: retries of the same bytes reuse it. */
export function partIdempotencyKey(uploadId: string, partNumber: number, sha256: string): string {
  return `part:${uploadId}:${partNumber}:${sha256.slice(0, 16)}`;
}

export class ApiClient {
  private readonly apiRoot: string;
  private readonly deviceId: string;
  private readonly fetchImpl: typeof fetch;
  private readonly requestTimeoutMs: number;
  private readonly maxAttempts: number;
  private readonly retryBaseDelayMs: number;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly log: (message: string) => void;

  constructor(options: ApiClientOptions) {
    this.apiRoot = `${options.baseUrl.replace(/\/+$/, '')}/api/v1`;
    this.deviceId = options.deviceId;
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.requestTimeoutMs = options.requestTimeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.maxAttempts = Math.max(1, options.maxAttempts ?? DEFAULT_MAX_ATTEMPTS);
    this.retryBaseDelayMs = Math.max(0, options.retryBaseDelayMs ?? DEFAULT_RETRY_BASE_MS);
    this.sleep = options.sleep ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
    this.log = options.log ?? (() => {});
  }

  // -- transport ---------------------------------------------------------

  private buildUrl(path: string, query?: RequestOptions['query']): string {
    const url = new URL(`${this.apiRoot}${path}`);
    if (query) {
      for (const [key, value] of Object.entries(query)) {
        if (value === undefined || value === null || value === '') continue;
        url.searchParams.set(key, String(value));
      }
    }
    return url.toString();
  }

  private async parseError(response: Response): Promise<ApiClientError> {
    let payload: unknown = null;
    try {
      payload = await response.json();
    } catch {
      /* non-JSON error body */
    }
    if (isRecord(payload) && isRecord(payload.error)) {
      const envelope = payload.error;
      return new ApiClientError(
        response.status,
        typeof envelope.code === 'string' ? envelope.code : `HTTP_${response.status}`,
        typeof envelope.message === 'string' ? envelope.message : response.statusText,
        isRecord(envelope.details) ? envelope.details : {},
      );
    }
    return new ApiClientError(
      response.status,
      `HTTP_${response.status}`,
      response.statusText || `Request failed with status ${response.status}`,
    );
  }

  private async request<T>(method: HttpMethod, path: string, options: RequestOptions = {}): Promise<T> {
    const url = this.buildUrl(path, options.query);
    const attempts = Math.max(1, options.maxAttempts ?? this.maxAttempts);
    let lastError: unknown = null;

    for (let attempt = 1; attempt <= attempts; attempt += 1) {
      const headers: Record<string, string> = {
        accept: 'application/json',
        'x-device-id': this.deviceId,
        'user-agent': '@pdf2x/mcp',
      };
      let body: string | undefined;
      if (options.body !== undefined) {
        headers['content-type'] = 'application/json';
        body = JSON.stringify(options.body);
      }
      if (options.idempotencyKey) {
        headers['idempotency-key'] = options.idempotencyKey;
      }

      let response: Response;
      try {
        response = await this.fetchImpl(url, {
          method,
          headers,
          body,
          signal: AbortSignal.timeout(options.timeoutMs ?? this.requestTimeoutMs),
        });
      } catch (error) {
        lastError = error;
        if (attempt < attempts) {
          await this.backoff(attempt, undefined);
          continue;
        }
        throw new ApiClientError(
          0,
          'NETWORK_ERROR',
          `Unable to reach the API at ${this.apiRoot}: ${(error as Error).message}`,
        );
      }

      if (response.ok) {
        if (response.status === 204) return undefined as T;
        const text = await response.text();
        if (text.length === 0) return undefined as T;
        try {
          return JSON.parse(text) as T;
        } catch {
          throw new ApiClientError(
            response.status,
            'INVALID_RESPONSE',
            'The API returned a body that is not valid JSON.',
          );
        }
      }

      const error = await this.parseError(response);
      if (error.retryable && attempt < attempts) {
        lastError = error;
        await this.backoff(attempt, response.headers.get('retry-after'));
        continue;
      }
      throw error;
    }

    const detail = lastError instanceof Error ? lastError.message : String(lastError);
    throw new ApiClientError(0, 'NETWORK_ERROR', `Request failed after ${attempts} attempts: ${detail}`);
  }

  private async backoff(attempt: number, retryAfter: string | null | undefined): Promise<void> {
    let delay = this.retryBaseDelayMs * 2 ** (attempt - 1);
    const seconds = retryAfter ? Number(retryAfter) : Number.NaN;
    if (Number.isFinite(seconds) && seconds > 0) delay = Math.min(seconds * 1000, 10_000);
    delay += Math.floor(Math.random() * 50);
    this.log(`transient failure; retrying in ${delay}ms`);
    await this.sleep(delay);
  }

  // -- batches -----------------------------------------------------------

  createBatch(clientBatchKey: string): Promise<BatchSummary> {
    return this.request<BatchSummary>('POST', '/batches', {
      body: { client_batch_key: clientBatchKey },
      idempotencyKey: `batch:${clientBatchKey}`,
    });
  }

  getBatch(batchId: string): Promise<BatchSummary> {
    return this.request<BatchSummary>('GET', `/batches/${encodeURIComponent(batchId)}`);
  }

  registerItem(batchId: string, input: RegisterBatchItemRequest): Promise<RegisterBatchItemResponse> {
    return this.request<RegisterBatchItemResponse>(
      'POST',
      `/batches/${encodeURIComponent(batchId)}/items`,
      {
        body: input,
        idempotencyKey: `item:${batchId}:${input.client_item_id}`,
      },
    );
  }

  listItems(
    batchId: string,
    params: { cursor?: string | null; limit?: number; status?: string | null } = {},
  ): Promise<CursorPage<BatchItem>> {
    return this.request<CursorPage<BatchItem>>(
      'GET',
      `/batches/${encodeURIComponent(batchId)}/items`,
      { query: { cursor: params.cursor, limit: params.limit, status: params.status } },
    );
  }

  /** Finds one item by its client id, walking pages (bounded). */
  async findItemByClientId(batchId: string, clientItemId: string): Promise<BatchItem | null> {
    let cursor: string | null = null;
    for (let page = 0; page < 50; page += 1) {
      const result = await this.listItems(batchId, { cursor, limit: 200 });
      const found = result.items.find((item) => item.client_item_id === clientItemId);
      if (found) return found;
      if (!result.next_cursor) return null;
      cursor = result.next_cursor;
    }
    return null;
  }

  sealBatch(batchId: string): Promise<SealBatchResponse> {
    return this.request<SealBatchResponse>('POST', `/batches/${encodeURIComponent(batchId)}/seal`, {
      idempotencyKey: `seal:${batchId}`,
    });
  }

  cancelBatch(batchId: string): Promise<BatchSummary> {
    return this.request<BatchSummary>('POST', `/batches/${encodeURIComponent(batchId)}/cancel`);
  }

  cancelItem(batchId: string, itemId: string): Promise<BatchItem> {
    return this.request<BatchItem>(
      'POST',
      `/batches/${encodeURIComponent(batchId)}/items/${encodeURIComponent(itemId)}/cancel`,
    );
  }

  retryItem(batchId: string, itemId: string, scope: RetryScope): Promise<RetryItemResponse> {
    return this.request<RetryItemResponse>(
      'POST',
      `/batches/${encodeURIComponent(batchId)}/items/${encodeURIComponent(itemId)}/retry`,
      { body: { scope } },
    );
  }

  // -- uploads -----------------------------------------------------------

  async putPart(
    uploadId: string,
    partNumber: number,
    data: Buffer | Readable,
    sha256: string,
  ): Promise<UploadPartReceipt> {
    const url = this.buildUrl(
      `/uploads/${encodeURIComponent(uploadId)}/parts/${encodeURIComponent(String(partNumber))}`,
    );
    const idempotencyKey = partIdempotencyKey(uploadId, partNumber, sha256);
    const isStream = !Buffer.isBuffer(data);
    const attempts = isStream ? 1 : this.maxAttempts;
    let lastError: unknown = null;

    for (let attempt = 1; attempt <= attempts; attempt += 1) {
      const headers: Record<string, string> = {
        accept: 'application/json',
        'content-type': 'application/octet-stream',
        'x-device-id': this.deviceId,
        'x-part-sha256': sha256,
        'idempotency-key': idempotencyKey,
        'user-agent': '@pdf2x/mcp',
      };
      let body: NonNullable<RequestInit['body']>;
      if (Buffer.isBuffer(data)) {
        headers['content-length'] = String(data.byteLength);
        body = new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
      } else {
        body = Readable.toWeb(data) as ReadableStream;
      }

      let response: Response;
      try {
        response = await this.fetchImpl(url, {
          method: 'PUT',
          headers,
          body,
          duplex: isStream ? 'half' : undefined,
          signal: AbortSignal.timeout(Math.max(this.requestTimeoutMs, 120_000)),
        } as RequestInit);
      } catch (error) {
        lastError = error;
        if (attempt < attempts) {
          await this.backoff(attempt, undefined);
          continue;
        }
        throw new ApiClientError(
          0,
          'NETWORK_ERROR',
          `Part ${partNumber} could not be uploaded: ${(error as Error).message}`,
        );
      }

      if (response.ok) {
        return (await response.json()) as UploadPartReceipt;
      }
      const error = await this.parseError(response);
      if (error.retryable && attempt < attempts) {
        lastError = error;
        await this.backoff(attempt, response.headers.get('retry-after'));
        continue;
      }
      throw error;
    }

    const detail = lastError instanceof Error ? lastError.message : String(lastError);
    throw new ApiClientError(0, 'NETWORK_ERROR', `Part upload failed: ${detail}`);
  }

  getUpload(uploadId: string): Promise<UploadSessionDetail> {
    return this.request<UploadSessionDetail>('GET', `/uploads/${encodeURIComponent(uploadId)}`);
  }

  completeUpload(uploadId: string, sha256: string, totalParts: number): Promise<CompleteUploadResponse> {
    return this.request<CompleteUploadResponse>(
      'POST',
      `/uploads/${encodeURIComponent(uploadId)}/complete`,
      {
        body: { sha256, total_parts: totalParts },
        idempotencyKey: `complete:${uploadId}:${sha256.slice(0, 16)}:${totalParts}`,
      },
    );
  }

  abortUpload(uploadId: string): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'DELETE',
      `/uploads/${encodeURIComponent(uploadId)}`,
    );
  }

  // -- results -----------------------------------------------------------

  getResult(conversionId: string): Promise<ResultSummary> {
    return this.request<ResultSummary>(
      'GET',
      `/conversions/${encodeURIComponent(conversionId)}/result`,
    );
  }

  getPreview(
    conversionId: string,
    params: { sheet?: string; offset?: number; limit?: number } = {},
  ): Promise<Preview> {
    return this.request<Preview>(
      'GET',
      `/conversions/${encodeURIComponent(conversionId)}/preview`,
      { query: { sheet: params.sheet, offset: params.offset, limit: params.limit } },
    );
  }

  listExports(conversionId: string): Promise<ExportList> {
    return this.request<ExportList>(
      'GET',
      `/conversions/${encodeURIComponent(conversionId)}/exports`,
    );
  }

  /**
   * Opens an export download as a stream. The response body is never buffered
   * here; the caller pipes it to disk (or drops it).
   */
  async downloadExport(
    conversionId: string,
    exportId: string,
  ): Promise<{ stream: Readable; filename: string; sha256: string | null; contentType: string | null }> {
    const url = this.buildUrl(
      `/conversions/${encodeURIComponent(conversionId)}/exports/${encodeURIComponent(exportId)}/download`,
    );

    let response: Response | null = null;
    let lastError: unknown = null;
    for (let attempt = 1; attempt <= this.maxAttempts; attempt += 1) {
      try {
        response = await this.fetchImpl(url, {
          method: 'GET',
          headers: {
            accept: '*/*',
            'x-device-id': this.deviceId,
            'user-agent': '@pdf2x/mcp',
          },
          signal: AbortSignal.timeout(Math.max(this.requestTimeoutMs, 300_000)),
        });
      } catch (error) {
        lastError = error;
        if (attempt < this.maxAttempts) {
          await this.backoff(attempt, undefined);
          continue;
        }
        throw new ApiClientError(
          0,
          'NETWORK_ERROR',
          `Download failed: ${(error as Error).message}`,
        );
      }

      if (response.ok) {
        if (!response.body) {
          throw new ApiClientError(response.status, 'INVALID_RESPONSE', 'The download body is empty.');
        }
        return {
          stream: Readable.fromWeb(response.body as Parameters<typeof Readable.fromWeb>[0]),
          filename: parseFilename(
            response.headers.get('content-disposition'),
            `${exportId}`,
          ),
          sha256: response.headers.get('x-content-sha256'),
          contentType: response.headers.get('content-type'),
        };
      }

      const error = await this.parseError(response);
      if (error.retryable && attempt < this.maxAttempts) {
        lastError = error;
        await this.backoff(attempt, response.headers.get('retry-after'));
        continue;
      }
      throw error;
    }

    const detail = lastError instanceof Error ? lastError.message : String(lastError);
    throw new ApiClientError(0, 'NETWORK_ERROR', `Download failed: ${detail}`);
  }
}

/** SHA-256 of a buffer, hex encoded. Used for part checksums. */
export function sha256Buffer(data: Buffer): string {
  return createHash('sha256').update(data).digest('hex');
}
