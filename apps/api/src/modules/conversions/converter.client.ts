import { Readable } from 'node:stream';

import { Inject, Injectable, Logger, Optional } from '@nestjs/common';
import type {
  AuditEvent,
  AuditIssue,
  AuditSummary,
  ConversionOptionsV2,
  ConversionStatus,
  PageAudit,
  Preview,
  QualityState,
  ResultOverview,
  ResultSummary,
  RunStats,
  SheetPage,
  SheetSourcePages,
  ValidationReport,
} from '@pdf2x/contracts';

import { ApiError } from '../../common/errors/api-error';
import { ERROR_CODES } from '../../common/errors/error-codes';
import type { ConversionStage } from '../../common/status';
import { AppConfigService } from '../../config/app-config.service';

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */

export interface ConverterErrorInfo {
  code: string;
  message: string;
  detail?: string | null;
  retryable?: boolean;
}

export interface ConverterJobEvent {
  type: string;
  status?: string | null;
  stage?: string | null;
  progress?: number | null;
  message?: string | null;
  payload?: Record<string, unknown> | null;
}

/** Normalised status document returned by the engine for a run. */
export interface ConverterJobState {
  run_id: string;
  conversion_id?: string | null;
  status: string;
  stage?: string | null;
  progress?: number | null;
  progress_indeterminate?: boolean | null;
  quality?: string | null;
  message?: string | null;
  stats?: RunStats | null;
  validation?: ValidationReport | null;
  error?: ConverterErrorInfo | null;
  engine_version?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  events?: ConverterJobEvent[];
}

export interface EnqueueOptions {
  /**
   * Canonical, fully normalised options. When present this is what the engine
   * receives, so no field is lost on the way (the previous shape only carried
   * `ocr`/`maxPages` and silently dropped TXT/CSV choices).
   */
  conversionOptions?: ConversionOptionsV2;
  ocr?: boolean;
  quality?: 'fast' | 'balanced' | 'thorough' | string;
  /** @deprecated Removed from the flow: documents are processed in full. */
  maxPages?: number;
  forceOcr?: boolean;
  sheetMode?: string;
  targetFormats?: string[];
}

export interface EnqueueRequest {
  runId: string;
  conversionId: string;
  storageKey: string;
  filename: string;
  options?: EnqueueOptions;
}

export interface CancelResult {
  /** `true` only when the engine confirmed the cancellation. */
  cancelled: boolean;
}

export interface EnqueueResult {
  accepted: boolean;
  run_id: string | null;
  status: string | null;
}

export interface ConverterHealth {
  status: string;
  engine_version: string | null;
  ocr_enabled: boolean | null;
  ocr_engine: string | null;
  queue: string | null;
  capabilities: Record<string, boolean>;
}

export interface DownloadPayload {
  stream: Readable;
  contentType: string;
  contentLength: number | null;
  filename: string;
}

export interface PreviewParams {
  sheet?: string;
  offset?: number;
  limit?: number;
}

/** Stringly query bag forwarded to the engine (audit/proxy endpoints). */
export type QueryParams = Record<string, string | number | boolean | undefined>;

export interface AuditEventsResponse {
  items: AuditEvent[];
  total: number;
}

export interface AuditCellsResponse {
  items: Array<Record<string, unknown>>;
  total: number;
}

/** One processed page, as reported by the engine (`GET /pages`). */
export interface PageSummary {
  page_number: number;
  width: number;
  height: number;
  rotation: number;
  classification: string;
  status: string;
  text_source: string;
  native_chars: number;
  ocr_chars: number;
  coverage_percent: number | null;
  regions: number;
}

export interface PageListResponse {
  items: PageSummary[];
  total: number;
}

/** One reconstructed table, as reported by the engine (`GET /tables`). */
export interface ReconstructedTable {
  table_id: string;
  page_number: number;
  sheet_name: string | null;
  strategy: string;
  headers: string[];
  rows: Array<Array<string | null>>;
  n_rows: number;
  n_cols: number;
  confidence: number;
  bbox: number[];
  merged: unknown[];
  continued_from: string | null;
  continued_on_page: number | null;
}

export interface TableListResponse {
  items: ReconstructedTable[];
  total: number;
}

export interface AuditIssuesResponse {
  items: AuditIssue[];
  total: number;
}

export interface AuditExportResponse {
  stream: Readable;
  contentType: string;
  filename: string;
}

export const CONVERTER_FETCH = 'CONVERTER_FETCH';

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;
type Json = Record<string, unknown>;

const DEFAULT_JOB_SUFFIX = '';

function isRecord(value: unknown): value is Json {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function toQueryString(params?: QueryParams): string {
  if (!params) return '';
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined) continue;
    search.set(key, String(value));
  }
  const serialised = search.toString();
  return serialised ? `?${serialised}` : '';
}

/* -------------------------------------------------------------------------- */
/* Client                                                                     */
/* -------------------------------------------------------------------------- */

/**
 * HTTP client for the Python engine (`services/converter`).
 *
 * Route family: `/internal/v1/jobs/{runId}/...`, authenticated with
 * `x-internal-token`. Never converts anything locally — the API is a proxy
 * plus the durable state machine.
 */
@Injectable()
export class ConverterClient {
  private readonly logger = new Logger(ConverterClient.name);
  private readonly baseUrl: string;
  private readonly token: string;
  private readonly timeoutMs: number;
  private readonly fetchImpl: FetchLike;

  constructor(
    config: AppConfigService,
    @Optional() @Inject(CONVERTER_FETCH) fetchImpl?: FetchLike,
  ) {
    this.baseUrl = config.converterInternalUrl.replace(/\/+$/, '');
    this.token = config.converterInternalToken;
    this.timeoutMs = config.converterTimeoutMs;
    this.fetchImpl = fetchImpl ?? ((input, init) => globalThis.fetch(input, init));
  }

  /* ----------------------------- lifecycle ------------------------------- */

  async enqueue(request: EnqueueRequest): Promise<EnqueueResult> {
    const body = {
      run_id: request.runId,
      conversion_id: request.conversionId,
      storage_key: request.storageKey,
      filename: request.filename,
      options: this.toEngineOptions(request.options),
    };

    const payload = await this.requestJson<Json>(
      request.runId,
      '/enqueue',
      { method: 'POST', body: JSON.stringify(body) },
    );

    return {
      accepted: payload['accepted'] !== false,
      run_id: typeof payload['run_id'] === 'string' ? payload['run_id'] : request.runId,
      status: typeof payload['status'] === 'string' ? payload['status'] : 'queued',
    };
  }

  /**
   * Asks the engine to cancel a run. `cancelled` is true only when the engine
   * confirmed it (the run had not been published yet). A false value means the
   * run already finished or is unknown to the engine.
   */
  async cancel(runId: string): Promise<CancelResult> {
    const payload = await this.requestJson<Json>(runId, '/cancel', { method: 'POST', body: '{}' });
    return { cancelled: payload['cancelled'] === true };
  }

  async status(runId: string): Promise<ConverterJobState> {
    const payload = await this.requestJson<Json>(runId, DEFAULT_JOB_SUFFIX);
    return this.normaliseState(runId, payload);
  }

  async result(runId: string): Promise<ResultSummary> {
    return this.requestJson<ResultSummary>(runId, '/result');
  }

  /** Compact overview: payload size does not grow with the cell count. */
  async resultOverview(runId: string): Promise<ResultOverview> {
    return this.requestJson<ResultOverview>(runId, '/result-summary');
  }

  /** Paginated sheet selector metadata (never the full page list). */
  async sheets(
    runId: string,
    params: { cursor?: string; limit?: number; q?: string } = {},
  ): Promise<SheetPage> {
    return this.requestJson<SheetPage>(
      runId,
      '/sheets',
      {},
      { cursor: params.cursor, limit: params.limit, q: params.q },
    );
  }

  /** Paginated per-sheet provenance pages. */
  async sheetPages(
    runId: string,
    sheetId: string,
    params: { cursor?: number; limit?: number } = {},
  ): Promise<SheetSourcePages> {
    return this.requestJson<SheetSourcePages>(
      runId,
      `/sheets/${encodeURIComponent(sheetId)}/pages`,
      {},
      { cursor: params.cursor, limit: params.limit },
    );
  }

  async preview(runId: string, params: PreviewParams = {}): Promise<Preview> {
    return this.requestJson<Preview>(runId, '/preview', {}, {
      sheet: params.sheet,
      offset: params.offset,
      limit: params.limit,
    });
  }

  async download(runId: string): Promise<DownloadPayload> {
    const { response, release } = await this.requestRaw(runId, '/download', { method: 'GET' });
    if (!response.body) {
      release();
      throw ApiError.converterUnavailable({ reason: 'empty_download_body', run_id: runId });
    }
    const stream = Readable.fromWeb(response.body as unknown as Parameters<typeof Readable.fromWeb>[0]);
    // The deadline covers the body until it is fully consumed (or fails).
    stream.once('close', release);
    return {
      stream,
      contentType:
        response.headers.get('content-type') ??
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      contentLength: this.readContentLength(response),
      filename: this.readFilename(response) ?? `conversao-${runId}.xlsx`,
    };
  }

  /* ------------------------------- audit --------------------------------- */

  async auditSummary(runId: string): Promise<AuditSummary> {
    return this.requestJson<AuditSummary>(runId, '/audit');
  }

  async auditEvents(runId: string, query: QueryParams = {}): Promise<AuditEventsResponse> {
    const payload = await this.requestJson<unknown>(runId, '/audit/events', {}, query);
    return this.toItems<AuditEvent>(payload, 'events');
  }

  async auditPage(runId: string, page: number): Promise<PageAudit> {
    return this.requestJson<PageAudit>(runId, `/audit/pages/${encodeURIComponent(String(page))}`);
  }

  async pages(runId: string): Promise<PageListResponse> {
    return this.requestJson<PageListResponse>(runId, '/pages');
  }

  async tables(runId: string): Promise<TableListResponse> {
    return this.requestJson<TableListResponse>(runId, '/tables');
  }

  async cellEvidence(runId: string, cellId: string): Promise<Record<string, unknown>> {
    return this.requestJson<Record<string, unknown>>(
      runId,
      `/cells/${encodeURIComponent(cellId)}`,
    );
  }

  async auditCells(runId: string, query: QueryParams = {}): Promise<AuditCellsResponse> {
    const payload = await this.requestJson<unknown>(runId, '/audit/cells', {}, query);
    return this.toItems<Record<string, unknown>>(payload, 'cells');
  }

  async auditIssues(runId: string, query: QueryParams = {}): Promise<AuditIssuesResponse> {
    const payload = await this.requestJson<unknown>(runId, '/audit/issues', {}, query);
    return this.toItems<AuditIssue>(payload, 'issues');
  }

  async auditExport(runId: string, query: QueryParams = {}): Promise<AuditExportResponse> {
    const { response, release } = await this.requestRaw(
      runId,
      '/audit/export',
      { method: 'GET' },
      query,
    );
    if (!response.body) {
      release();
      throw ApiError.converterUnavailable({ reason: 'empty_audit_export_body', run_id: runId });
    }
    const stream = Readable.fromWeb(response.body as unknown as Parameters<typeof Readable.fromWeb>[0]);
    stream.once('close', release);
    return {
      stream,
      contentType: response.headers.get('content-type') ?? 'application/json',
      filename: this.readFilename(response) ?? `auditoria-${runId}.json`,
    };
  }

  /* ------------------------------- system -------------------------------- */

  async health(): Promise<ConverterHealth> {
    const payload = await this.requestJson<Json>(null, null, { method: 'GET' }, undefined, '/health');
    const capabilities = isRecord(payload['capabilities'])
      ? Object.fromEntries(
          Object.entries(payload['capabilities']).map(([key, value]) => [key, Boolean(value)]),
        )
      : {};

    return {
      status: typeof payload['status'] === 'string' ? payload['status'] : 'unknown',
      engine_version:
        typeof payload['engine_version'] === 'string' ? payload['engine_version'] : null,
      ocr_enabled: typeof payload['ocr_enabled'] === 'boolean' ? payload['ocr_enabled'] : null,
      ocr_engine: typeof payload['ocr_engine'] === 'string' ? payload['ocr_engine'] : null,
      queue: typeof payload['queue'] === 'string' ? payload['queue'] : null,
      capabilities,
    };
  }

  /** Health probe that never throws (settings/health endpoints). */
  async safeHealth(): Promise<ConverterHealth | null> {
    try {
      return await this.health();
    } catch (error) {
      this.logger.debug(
        `Motor de conversão indisponível: ${error instanceof Error ? error.message : String(error)}`,
      );
      return null;
    }
  }

  /* ------------------------------ transport ------------------------------ */

  /** Path (not URL) of a job resource inside the internal API. */
  private jobPath(runId: string, suffix: string): string {
    return `/jobs/${encodeURIComponent(runId)}${suffix}`;
  }

  private async requestJson<T>(
    runId: string | null,
    suffix: string | null,
    init: RequestInit = {},
    query?: QueryParams,
    absolutePath?: string,
  ): Promise<T> {
    const { response, url, release } = await this.requestRaw(
      runId,
      suffix,
      init,
      query,
      absolutePath,
    );
    try {
      // The body is read under the same deadline as the headers (A-26).
      const text = await response.text();
      if (!text) return {} as T;
      try {
        return JSON.parse(text) as T;
      } catch {
        throw ApiError.converterUnavailable({
          reason: 'invalid_json_from_converter',
          run_id: runId,
          status: response.status,
        });
      }
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw this.transportError(error, url);
    } finally {
      release();
    }
  }

  /**
   * Sends the request under `CONVERTER_TIMEOUT_MS`. The returned `release`
   * must be called once the body has been read (or abandoned); until then the
   * deadline stays armed, so a stalled body cannot hang the caller.
   */
  private async requestRaw(
    runId: string | null,
    suffix: string | null,
    init: RequestInit = {},
    query?: QueryParams,
    absolutePath?: string,
  ): Promise<{ response: Response; url: string; release: () => void }> {
    const path = absolutePath ?? this.jobPath(runId ?? '', suffix ?? DEFAULT_JOB_SUFFIX);
    const url = `${this.baseUrl}/internal/v1${path}${toQueryString(query)}`;

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    let released = false;
    const release = (): void => {
      if (released) return;
      released = true;
      clearTimeout(timer);
    };

    try {
      const response = await this.fetchImpl(url, {
        ...init,
        headers: {
          Accept: 'application/json',
          'x-internal-token': this.token,
          ...(init.body ? { 'Content-Type': 'application/json' } : {}),
          ...(init.headers as Record<string, string> | undefined),
        },
        signal: controller.signal,
      });

      if (!response.ok) {
        throw await this.toApiError(response, url);
      }

      return { response, url, release };
    } catch (error) {
      release();
      if (error instanceof ApiError) throw error;
      throw this.transportError(error, url);
    }
  }

  private transportError(error: unknown, url: string): ApiError {
    if (this.isAbortError(error)) {
      return ApiError.converterUnavailable({
        reason: 'timeout',
        timeout_ms: this.timeoutMs,
        url,
      });
    }
    return ApiError.converterUnavailable({
      reason: 'network_error',
      url,
      detail: error instanceof Error ? error.message : String(error),
    });
  }

  private async toApiError(response: Response, url: string): Promise<ApiError> {
    const status = response.status;
    let detail: string | null = null;
    try {
      const text = await response.text();
      detail = text ? text.slice(0, 500) : null;
    } catch {
      detail = null;
    }

    if (status === 404) {
      return ApiError.notFound({ reason: 'converter_not_found', url, detail });
    }
    if (status === 409 || status === 412 || status === 425) {
      return ApiError.conflict(ERROR_CODES.NOT_READY, {
        reason: 'converter_not_ready',
        url,
        detail,
      });
    }
    if (status === 401 || status === 403) {
      return ApiError.converterUnavailable({
        reason: 'converter_unauthorized',
        url,
        detail,
        hint: 'CONVERTER_INTERNAL_TOKEN divergente entre API e motor',
      });
    }
    if (status === 422) {
      return ApiError.validation({ reason: 'converter_validation_error', url, detail });
    }
    if (status >= 500) {
      return ApiError.converterUnavailable({
        reason: 'converter_error',
        upstream_status: status,
        url,
        detail,
      });
    }
    return ApiError.internal({ reason: 'unexpected_converter_status', upstream_status: status, url });
  }

  private normaliseState(runId: string, payload: Json): ConverterJobState {
    const stats = isRecord(payload['stats']) ? (payload['stats'] as unknown as RunStats) : null;
    const validation = isRecord(payload['validation'])
      ? (payload['validation'] as unknown as ValidationReport)
      : null;

    const error = isRecord(payload['error'])
      ? {
          code: typeof payload['error']['code'] === 'string' ? payload['error']['code'] : 'unknown',
          message:
            typeof payload['error']['message'] === 'string'
              ? payload['error']['message']
              : 'Falha na conversão.',
          detail:
            typeof payload['error']['detail'] === 'string' ? payload['error']['detail'] : null,
          retryable: payload['error']['retryable'] !== false,
        }
      : null;

    const events: ConverterJobEvent[] = Array.isArray(payload['events'])
      ? payload['events']
          .filter(isRecord)
          .map((event) => ({
            type: typeof event['type'] === 'string' ? event['type'] : 'log',
            status: typeof event['status'] === 'string' ? event['status'] : null,
            stage: typeof event['stage'] === 'string' ? event['stage'] : null,
            progress: typeof event['progress'] === 'number' ? event['progress'] : null,
            message: typeof event['message'] === 'string' ? event['message'] : null,
            payload: isRecord(event['payload']) ? event['payload'] : null,
          }))
      : [];

    return {
      run_id: typeof payload['run_id'] === 'string' ? payload['run_id'] : runId,
      conversion_id:
        typeof payload['conversion_id'] === 'string' ? payload['conversion_id'] : null,
      status: typeof payload['status'] === 'string' ? payload['status'] : 'unknown',
      stage: typeof payload['stage'] === 'string' ? payload['stage'] : null,
      progress: typeof payload['progress'] === 'number' ? payload['progress'] : null,
      progress_indeterminate:
        typeof payload['progress_indeterminate'] === 'boolean'
          ? payload['progress_indeterminate']
          : null,
      quality: typeof payload['quality'] === 'string' ? payload['quality'] : null,
      message: typeof payload['message'] === 'string' ? payload['message'] : null,
      stats,
      validation,
      error,
      engine_version:
        typeof payload['engine_version'] === 'string' ? payload['engine_version'] : null,
      started_at: typeof payload['started_at'] === 'string' ? payload['started_at'] : null,
      finished_at: typeof payload['finished_at'] === 'string' ? payload['finished_at'] : null,
      events,
    };
  }

  private toItems<T>(payload: unknown, key: string): { items: T[]; total: number } {
    if (Array.isArray(payload)) {
      return { items: payload as T[], total: payload.length };
    }
    if (isRecord(payload)) {
      const items = Array.isArray(payload['items']) ? (payload['items'] as T[]) : [];
      const total = typeof payload['total'] === 'number' ? payload['total'] : items.length;
      return { items, total };
    }
    this.logger.warn(`Resposta inesperada do motor para ${key}`);
    return { items: [], total: 0 };
  }

  private toEngineOptions(options?: EnqueueOptions): Json {
    if (!options) return {};

    // The canonical payload goes over the wire untouched. The engine treats it
    // as `schema_version: 2` and never applies a page ceiling.
    if (options.conversionOptions) {
      return {
        ...options.conversionOptions,
        ocr: { ...options.conversionOptions.ocr },
        text: { ...options.conversionOptions.text },
        csv: { ...options.conversionOptions.csv },
        target_formats: [...options.conversionOptions.target_formats],
      } as unknown as Json;
    }

    // Legacy callers that only speak the flat shape.
    return {
      ...(options.ocr === undefined ? {} : { ocr: options.ocr }),
      ...(options.quality === undefined ? {} : { quality: options.quality }),
      ...(options.maxPages === undefined ? {} : { max_pages: options.maxPages }),
      ...(options.forceOcr === undefined ? {} : { force_ocr: options.forceOcr }),
      ...(options.sheetMode === undefined ? {} : { sheet_mode: options.sheetMode }),
      ...(options.targetFormats === undefined ? {} : { target_formats: options.targetFormats }),
    };
  }

  private isAbortError(error: unknown): boolean {
    if (!isRecord(error)) return false;
    return error['name'] === 'AbortError' || error['code'] === 'ABORT_ERR';
  }

  private readContentLength(response: Response): number | null {
    const raw = response.headers.get('content-length');
    if (!raw) return null;
    const parsed = Number.parseInt(raw, 10);
    return Number.isFinite(parsed) ? parsed : null;
  }

  private readFilename(response: Response): string | null {
    const disposition = response.headers.get('content-disposition');
    if (!disposition) return null;
    const utf8 = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
    if (utf8) {
      try {
        return decodeURIComponent(utf8[1]);
      } catch {
        return utf8[1];
      }
    }
    const plain = /filename="?([^";]+)"?/i.exec(disposition);
    return plain ? plain[1] : null;
  }

  /** Exposed for the tracker: turns a raw engine status into a contract status. */
  static isTerminalEngineStatus(status: string): boolean {
    return ['completed', 'completed_with_warnings', 'failed', 'cancelled'].includes(status);
  }

  static asStage(value: string | null | undefined): ConversionStage | null {
    if (!value) return null;
    const allowed: readonly string[] = [
      'receive',
      'inspect',
      'extract',
      'structure',
      'normalize',
      'reconcile',
      'generate',
      'audit',
      'done',
      'failed',
    ];
    return allowed.includes(value) ? (value as ConversionStage) : null;
  }

  static asStatus(value: string | null | undefined): ConversionStatus | null {
    if (!value) return null;
    const allowed: readonly string[] = [
      'received',
      'validating',
      'queued',
      'inspecting',
      'extracting',
      'structuring',
      'normalizing',
      'reconciling',
      'generating',
      'auditing',
      'completed',
      'completed_with_warnings',
      'failed',
      'cancelled',
    ];
    return allowed.includes(value) ? (value as ConversionStatus) : null;
  }

  static asQuality(value: string | null | undefined): QualityState | null {
    if (!value) return null;
    const allowed: readonly string[] = ['validated', 'partial', 'failed', 'pending'];
    return allowed.includes(value) ? (value as QualityState) : null;
  }
}
