/**
 * In-memory implementation of the `/api/v1` batch/upload surface, used by the
 * adapter tests. It follows `services/converter/api/batch_service.py` and
 * `upload_service.py` for status derivation, idempotency and error codes.
 */

import { createHash, randomBytes } from 'node:crypto';
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';

export interface MockOptions {
  /** Part size handed out by registration (small on purpose for tests). */
  partSize: number;
  /** Simulate transport corruption for the next N part uploads. */
  corruptParts?: number;
  /** Reject part uploads whose number is >= this with HTTP 500 (until reset). */
  failPartsAtOrAfter?: number | null;
  /** Maximum PDF bytes accepted (0 = no limit). */
  maxFileBytes?: number;
}

interface MockItem {
  item_id: string;
  batch_id: string;
  client_item_id: string;
  ordinal: number;
  filename: string;
  size_bytes: number;
  kind: 'pdf' | 'image';
  status: string;
  options: Record<string, unknown>;
  fingerprint: string;
  upload_id: string | null;
  conversion_id: string | null;
  error: { code: string; message: string; details?: unknown } | null;
}

interface MockBatch {
  batch_id: string;
  client_batch_key: string;
  owner: string;
  status: string;
  sealed_at: string | null;
  cancel_requested_at: string | null;
  version: number;
  created_at: string;
}

interface MockPart {
  part_number: number;
  size: number;
  sha256: string;
  data: Buffer;
}

interface MockUpload {
  upload_id: string;
  item_id: string;
  filename: string;
  state: string;
  expected_bytes: number;
  received_bytes: number;
  sha256: string | null;
  part_size: number;
  parts: Map<number, MockPart>;
  created_at: string;
}

interface MockConversion {
  conversion_id: string;
  item_id: string;
  status: string;
  filename: string;
  sha256: string;
  options: Record<string, unknown>;
  exports: MockExport[];
}

interface MockExport {
  export_id: string;
  format: string;
  filename: string;
  mime_type: string;
  sha256: string;
  size_bytes: number;
  publication_status: 'approved' | 'quarantined';
  data: Buffer;
  created_at: string;
}

const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'bmp', 'webp', 'tif', 'tiff', 'gif']);

function declaredKind(filename: string): 'pdf' | 'image' | 'unknown' {
  const base = filename.replace(/\\/g, '/').split('/').pop() ?? filename;
  const dot = base.lastIndexOf('.');
  const extension = dot >= 0 ? base.slice(dot + 1).toLowerCase() : '';
  if (extension === 'pdf') return 'pdf';
  if (IMAGE_EXTENSIONS.has(extension)) return 'image';
  return 'unknown';
}

function normalizeOptions(
  options: Record<string, unknown> | null | undefined,
  kind: 'pdf' | 'image' | 'unknown',
): Record<string, unknown> {
  const target = options?.target_formats;
  let targetFormats = Array.isArray(target) ? target.map(String) : null;
  if (targetFormats) {
    const invalid = targetFormats.filter((format) => !['xlsx', 'txt', 'csv'].includes(format));
    if (invalid.length > 0) throw new MockApiError(400, 'INVALID_OPTION', 'Formato inválido.');
  }
  if (kind === 'image') {
    if (targetFormats && targetFormats.some((format) => format !== 'txt')) {
      throw new MockApiError(
        400,
        'INCOMPATIBLE_OUTPUT_FORMAT',
        'Imagens só podem ser entregues como TXT.',
        { formats: targetFormats, allowed: ['txt'] },
      );
    }
    targetFormats = ['txt'];
  }
  if (!targetFormats) targetFormats = ['xlsx', 'txt', 'csv'];
  return {
    schema_version: 2,
    target_formats: targetFormats,
    ocr: { enabled: true, languages: ['por', 'eng'], dpi: null, strategy: 'auto' },
    text: { newline: 'lf', page_separator: 'form_feed', include_table_text: true },
    csv: {
      profile: 'interoperable',
      delimiter: null,
      formula_policy: 'safe',
      table_ids: null,
      value_mode: 'original',
    },
  };
}

function stableJson(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value) ?? 'null';
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  const record = value as Record<string, unknown>;
  const keys = Object.keys(record)
    .filter((key) => record[key] !== undefined)
    .sort();
  return `{${keys.map((key) => `${JSON.stringify(key)}:${stableJson(record[key])}`).join(',')}}`;
}

function sha256(data: Buffer): string {
  return createHash('sha256').update(data).digest('hex');
}

class MockApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

function iso(date = new Date()): string {
  return date.toISOString();
}

function parseBody(request: IncomingMessage): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    request.on('data', (chunk: Buffer) => chunks.push(chunk));
    request.on('end', () => resolve(Buffer.concat(chunks)));
    request.on('error', reject);
  });
}

export class MockApi {
  readonly partSize: number;
  readonly maxFileBytes: number;
  failPartsAtOrAfter: number | null;
  corruptParts: number;
  readonly batches = new Map<string, MockBatch>();
  readonly items = new Map<string, MockItem>();
  readonly uploads = new Map<string, MockUpload>();
  readonly conversions = new Map<string, MockConversion>();
  readonly deviceIds = new Set<string>();
  readonly partPuts: Array<{ upload_id: string; part_number: number }> = [];
  readonly requests: string[] = [];
  batchCreates = 0;

  private server: Server | null = null;
  private baseUrl = '';

  constructor(options: Partial<MockOptions> = {}) {
    this.partSize = options.partSize ?? 8;
    this.maxFileBytes = options.maxFileBytes ?? 0;
    this.failPartsAtOrAfter = options.failPartsAtOrAfter ?? null;
    this.corruptParts = options.corruptParts ?? 0;
  }

  get url(): string {
    return this.baseUrl;
  }

  async start(): Promise<string> {
    this.server = createServer((request, response) => {
      void this.handle(request, response);
    });
    await new Promise<void>((resolve) => {
      this.server?.listen(0, '127.0.0.1', () => resolve());
    });
    const address = this.server.address();
    if (!address || typeof address === 'string') throw new Error('mock server has no port');
    this.baseUrl = `http://127.0.0.1:${address.port}`;
    return this.baseUrl;
  }

  async stop(): Promise<void> {
    if (!this.server) return;
    await new Promise<void>((resolve) => this.server?.close(() => resolve()));
    this.server = null;
  }

  // -- helpers ----------------------------------------------------------

  private owner(request: IncomingMessage): string {
    const device = request.headers['x-device-id'];
    const value = typeof device === 'string' && device.length > 0 ? device : 'anonymous';
    this.deviceIds.add(value);
    return value;
  }

  private itemPayload(item: MockItem): Record<string, unknown> {
    return {
      item_id: item.item_id,
      batch_id: item.batch_id,
      client_item_id: item.client_item_id,
      ordinal: item.ordinal,
      filename: item.filename,
      size_bytes: item.size_bytes,
      kind: item.kind,
      status: item.status,
      options: item.options,
      available_formats: (item.options.target_formats as string[]) ?? [],
      upload_id: item.upload_id,
      conversion_id: item.conversion_id,
      pages_total: item.conversion_id ? 1 : null,
      pages_attempted: item.conversion_id ? 1 : null,
      pages_completed: item.conversion_id ? 1 : null,
      pages_failed: 0,
      phase: item.conversion_id ? 'conversion' : 'upload',
      error: item.error,
      created_at: iso(),
      updated_at: iso(),
      links: {},
    };
  }

  private batchPayload(batch: MockBatch): Record<string, unknown> {
    const items = [...this.items.values()].filter((item) => item.batch_id === batch.batch_id);
    const count = (status: string) => items.filter((item) => item.status === status).length;
    const counts = {
      total_items: items.length,
      pending_upload: count('pending_upload'),
      uploading: count('uploading'),
      queued: count('queued'),
      processing: 0,
      completed: count('completed'),
      with_warnings: 0,
      failed: count('failed'),
      cancelled: count('cancelled'),
    };
    const finished = counts.completed + counts.failed + counts.cancelled;
    return {
      batch_id: batch.batch_id,
      status: this.deriveBatchStatus(batch),
      client_batch_key: batch.client_batch_key,
      counts,
      progress: {
        finished_items: finished,
        pages_total: items.length,
        pages_attempted: finished,
        pages_completed: counts.completed,
        pages_failed: counts.failed,
      },
      created_at: batch.created_at,
      sealed_at: batch.sealed_at,
      cancel_requested_at: batch.cancel_requested_at,
      version: batch.version,
      links: {},
    };
  }

  private deriveBatchStatus(batch: MockBatch): string {
    if (batch.cancel_requested_at && batch.status === 'cancelled') return 'cancelled';
    const items = [...this.items.values()].filter((item) => item.batch_id === batch.batch_id);
    if (batch.sealed_at) {
      const finished = items.filter((item) =>
        ['completed', 'failed', 'cancelled'].includes(item.status),
      );
      if (finished.length === items.length) {
        return items.some((item) => item.status === 'failed' || item.status === 'cancelled')
          ? 'completed_with_errors'
          : 'completed';
      }
      return 'processing';
    }
    return 'accepting';
  }

  private expectedParts(expectedBytes: number): number {
    return Math.ceil(expectedBytes / this.partSize);
  }

  // -- dispatch ---------------------------------------------------------

  private async handle(request: IncomingMessage, response: ServerResponse): Promise<void> {
    this.requests.push(`${request.method} ${request.url}`);
    try {
      const body = await parseBody(request);
      const result = await this.route(request, body, response);
      if (result === null) return; // already written (download)
      response.writeHead(result.status, { 'content-type': 'application/json' });
      response.end(result.body === undefined ? '' : JSON.stringify(result.body));
    } catch (error) {
      if (error instanceof MockApiError) {
        response.writeHead(error.status, { 'content-type': 'application/json' });
        response.end(
          JSON.stringify({
            error: { code: error.code, message: error.message, details: error.details },
          }),
        );
        return;
      }
      response.writeHead(500, { 'content-type': 'application/json' });
      response.end(
        JSON.stringify({
          error: { code: 'INTERNAL_ERROR', message: (error as Error).message, details: {} },
        }),
      );
    }
  }

  private async route(
    request: IncomingMessage,
    body: Buffer,
    response: ServerResponse,
  ): Promise<{ status: number; body?: unknown } | null> {
    const method = request.method ?? 'GET';
    const url = new URL(request.url ?? '/', 'http://mock');
    const path = url.pathname.replace(/^\/api\/v1/, '');
    const owner = this.owner(request);
    const segments = path.split('/').filter(Boolean);
    const contentType = String(request.headers['content-type'] ?? '');
    const json =
      body.length > 0 && contentType.includes('application/json')
        ? (JSON.parse(body.toString('utf8')) as Record<string, unknown>)
        : {};

    // POST /batches
    if (method === 'POST' && segments.length === 1 && segments[0] === 'batches') {
      const key = String(json.client_batch_key ?? '');
      if (!key) throw new MockApiError(400, 'INVALID_OPTION', 'client_batch_key é obrigatório.');
      const existing = [...this.batches.values()].find(
        (batch) => batch.owner === owner && batch.client_batch_key === key,
      );
      if (existing) return { status: 200, body: this.batchPayload(existing) };
      this.batchCreates += 1;
      const batch: MockBatch = {
        batch_id: `batch_${randomBytes(6).toString('hex')}`,
        client_batch_key: key,
        owner,
        status: 'accepting',
        sealed_at: null,
        cancel_requested_at: null,
        version: 1,
        created_at: iso(),
      };
      this.batches.set(batch.batch_id, batch);
      return { status: 201, body: this.batchPayload(batch) };
    }

    if (segments[0] === 'batches' && segments.length >= 2) {
      const batch = this.batches.get(segments[1]!);
      if (!batch) throw new MockApiError(404, 'NOT_FOUND', 'Lote não encontrado.');
      if (batch.owner !== owner) throw new MockApiError(403, 'FORBIDDEN', 'Sem permissão.');

      if (method === 'GET' && segments.length === 2) {
        return { status: 200, body: this.batchPayload(batch) };
      }

      if (segments[2] === 'items' && segments.length === 3) {
        if (method === 'POST') return { status: 201, body: this.registerItem(batch, json) };
        if (method === 'GET') {
          const all = [...this.items.values()]
            .filter((item) => item.batch_id === batch.batch_id)
            .sort((a, b) => a.ordinal - b.ordinal);
          const offset = Number(url.searchParams.get('cursor') ?? 0) || 0;
          const limit = Math.min(Number(url.searchParams.get('limit') ?? 50) || 50, 200);
          const statusFilter = url.searchParams.get('status');
          const filtered = statusFilter
            ? all.filter((item) => item.status === statusFilter)
            : all;
          const slice = filtered.slice(offset, offset + limit);
          const next = offset + slice.length < filtered.length ? String(offset + slice.length) : null;
          return {
            status: 200,
            body: { items: slice.map((item) => this.itemPayload(item)), next_cursor: next },
          };
        }
      }

      if (segments[2] === 'items' && segments.length === 5) {
        const item = this.items.get(segments[3]!);
        if (!item || item.batch_id !== batch.batch_id) {
          throw new MockApiError(404, 'NOT_FOUND', 'Item não encontrado.');
        }
        if (method === 'POST' && segments[4] === 'cancel') {
          item.status = 'cancelled';
          return { status: 200, body: this.itemPayload(item) };
        }
        if (method === 'POST' && segments[4] === 'retry') {
          const scope = String(json.scope ?? 'conversion');
          if (!['upload', 'conversion'].includes(scope)) {
            throw new MockApiError(400, 'INVALID_OPTION', "scope deve ser 'upload' ou 'conversion'.");
          }
          if (scope === 'upload') {
            if (!item.upload_id) {
              throw new MockApiError(409, 'UPLOAD_NOT_FOUND', 'Não há sessão de upload.');
            }
            const upload = this.uploads.get(item.upload_id)!;
            upload.state = 'uploading';
            item.status = 'uploading';
            return {
              status: 202,
              body: {
                item: this.itemPayload(item),
                upload_id: upload.upload_id,
                next_part: this.nextPart(upload),
                part_size: upload.part_size,
              },
            };
          }
          if (!item.conversion_id) {
            throw new MockApiError(409, 'NOT_READY', 'Sem conversão para repetir.');
          }
          item.status = 'queued';
          return { status: 202, body: { item: this.itemPayload(item) } };
        }
      }

      if (segments[2] === 'seal' && method === 'POST') {
        if (batch.sealed_at) {
          return {
            status: 200,
            body: {
              batch_id: batch.batch_id,
              status: this.deriveBatchStatus(batch),
              sealed_at: batch.sealed_at,
              message: 'O lote já estava selado.',
            },
          };
        }
        const pending = [...this.items.values()].filter(
          (item) =>
            item.batch_id === batch.batch_id &&
            ['pending_upload', 'uploading'].includes(item.status),
        ).length;
        if (pending > 0) {
          throw new MockApiError(
            409,
            'BATCH_HAS_PENDING_UPLOADS',
            `O lote ainda possui ${pending} upload(s) pendente(s).`,
            { pending_uploads: pending },
          );
        }
        batch.sealed_at = iso();
        batch.version += 1;
        return {
          status: 200,
          body: {
            batch_id: batch.batch_id,
            status: this.deriveBatchStatus(batch),
            sealed_at: batch.sealed_at,
            message: 'Lote selado.',
          },
        };
      }

      if (segments[2] === 'cancel' && method === 'POST') {
        batch.cancel_requested_at = iso();
        const items = [...this.items.values()].filter((item) => item.batch_id === batch.batch_id);
        for (const item of items) {
          if (!['completed', 'failed', 'cancelled'].includes(item.status)) item.status = 'cancelled';
        }
        batch.status = 'cancelled';
        return { status: 200, body: this.batchPayload(batch) };
      }
    }

    if (segments[0] === 'uploads' && segments.length >= 2) {
      const upload = this.uploads.get(segments[1]!);
      if (!upload) throw new MockApiError(404, 'UPLOAD_NOT_FOUND', 'Sessão não encontrada.');
      if (method === 'GET' && segments.length === 2) {
        return { status: 200, body: this.uploadPayload(upload) };
      }
      if (method === 'PUT' && segments[2] === 'parts' && segments.length === 4) {
        return { status: 200, body: this.putPart(upload, Number(segments[3]), body, request) };
      }
      if (method === 'POST' && segments[2] === 'complete') {
        return { status: 202, body: this.complete(upload, json) };
      }
      if (method === 'DELETE' && segments.length === 2) {
        upload.state = 'aborted';
        upload.parts.clear();
        const item = this.items.get(upload.item_id);
        if (item && !['completed', 'failed', 'cancelled'].includes(item.status)) {
          item.status = 'upload_failed';
          item.upload_id = null;
          item.error = { code: 'UPLOAD_ABORTED', message: 'Upload abortado.' };
        }
        return { status: 200, body: { upload_id: upload.upload_id, state: upload.state, deleted_parts: 0 } };
      }
    }

    if (segments[0] === 'conversions' && segments.length >= 2) {
      const conversion = this.conversions.get(segments[1]!);
      if (!conversion) throw new MockApiError(404, 'NOT_FOUND', 'Conversão não encontrada.');
      if (method === 'GET' && segments[2] === 'result') {
        return {
          status: 200,
          body: {
            conversion_id: conversion.conversion_id,
            status: 'completed',
            quality: 'validated',
            pages: 1,
            tables: 1,
            sheets_count: 1,
            cells: 4,
            records: 2,
            fields: 2,
            download_url: `/api/v1/conversions/${conversion.conversion_id}/download`,
            audit_url: `/api/v1/conversions/${conversion.conversion_id}/audit`,
            sheets: [
              {
                name: 'Dados',
                kind: 'records',
                rows: 2,
                columns: 2,
                source_pages: [1],
                table_id: 'table_1',
              },
            ],
            warnings: [],
          },
        };
      }
      if (method === 'GET' && segments[2] === 'preview') {
        return {
          status: 200,
          body: {
            sheet: url.searchParams.get('sheet') ?? 'Dados',
            headers: ['Campo', 'Valor'],
            rows: [
              [
                { value: 'a', type: 'text', display: 'a' },
                { value: 1, type: 'integer', display: '1' },
              ],
              [
                { value: 'b', type: 'text', display: 'b' },
                { value: 2, type: 'integer', display: '2' },
              ],
            ],
            total_rows: 2,
            offset: Number(url.searchParams.get('offset') ?? 0),
            limit: Number(url.searchParams.get('limit') ?? 100),
          },
        };
      }
      if (method === 'GET' && segments[2] === 'exports' && segments.length === 3) {
        return {
          status: 200,
          body: {
            items: conversion.exports.map((artifact) => ({
              export_id: artifact.export_id,
              format: artifact.format,
              filename: artifact.filename,
              mime_type: artifact.mime_type,
              sha256: artifact.sha256,
              size_bytes: artifact.size_bytes,
              warnings: [],
              options: {},
              publication_status: artifact.publication_status,
              created_at: artifact.created_at,
              download_url:
                artifact.publication_status === 'approved'
                  ? `/api/v1/conversions/${conversion.conversion_id}/exports/${artifact.export_id}/download`
                  : null,
            })),
            total: conversion.exports.length,
          },
        };
      }
      if (
        method === 'GET' &&
        segments[2] === 'exports' &&
        segments.length === 5 &&
        segments[4] === 'download'
      ) {
        const artifact = conversion.exports.find((entry) => entry.export_id === segments[3]);
        if (!artifact) throw new MockApiError(404, 'NOT_FOUND', 'Exportação não encontrada.');
        if (artifact.publication_status !== 'approved') {
          throw new MockApiError(409, 'ARTIFACT_QUARANTINED', 'Artefato reprovado.');
        }
        response.writeHead(200, {
          'content-type': artifact.mime_type,
          'content-disposition': `attachment; filename="${artifact.filename}"`,
          'content-length': String(artifact.data.length),
          'x-content-sha256': artifact.sha256,
        });
        response.end(artifact.data);
        return null;
      }
    }

    throw new MockApiError(404, 'NOT_FOUND', `No mock route for ${method} ${path}`);
  }

  private registerItem(batch: MockBatch, json: Record<string, unknown>): Record<string, unknown> {
    if (batch.sealed_at) {
      throw new MockApiError(409, 'BATCH_NOT_OPEN', 'Este lote não aceita mais novos itens.');
    }
    if (batch.cancel_requested_at) {
      throw new MockApiError(409, 'BATCH_CANCELLED', 'O lote foi cancelado.');
    }
    const clientItemId = String(json.client_item_id ?? '');
    const filename = String(json.filename ?? '').replace(/[\\/]/g, '');
    const size = Number(json.size_bytes ?? 0);
    const kind = declaredKind(filename);
    if (kind === 'unknown') {
      throw new MockApiError(400, 'INVALID_FILE', 'Envie um PDF ou uma imagem.');
    }
    if (size <= 0) throw new MockApiError(400, 'INVALID_FILE', 'O arquivo está vazio.');
    if (this.maxFileBytes > 0 && size > this.maxFileBytes) {
      throw new MockApiError(400, 'FILE_TOO_LARGE', 'Arquivo grande demais.');
    }

    const options = normalizeOptions(
      (json.options as Record<string, unknown> | null | undefined) ?? null,
      kind,
    );
    const fingerprint = sha256(
      Buffer.from(stableJson({ options, filename, size_bytes: size }), 'utf8'),
    );

    const existing = [...this.items.values()].find(
      (item) => item.batch_id === batch.batch_id && item.client_item_id === clientItemId,
    );
    if (existing) {
      if (existing.fingerprint !== fingerprint) {
        throw new MockApiError(409, 'IDEMPOTENT_REPLAY', 'Item registrado com outros dados.');
      }
      const upload = existing.upload_id ? this.uploads.get(existing.upload_id) : null;
      return {
        item: this.itemPayload(existing),
        upload_id: existing.upload_id,
        part_size: upload?.part_size ?? this.partSize,
        next_part: upload ? this.nextPart(upload) : 0,
        replayed: true,
        expires_at: upload ? iso(new Date(Date.now() + 3_600_000)) : null,
      };
    }

    const ordinal =
      Math.max(
        0,
        ...[...this.items.values()]
          .filter((item) => item.batch_id === batch.batch_id)
          .map((item) => item.ordinal),
      ) + 1;
    const item: MockItem = {
      item_id: `item_${randomBytes(6).toString('hex')}`,
      batch_id: batch.batch_id,
      client_item_id: clientItemId,
      ordinal,
      filename,
      size_bytes: size,
      kind,
      status: 'pending_upload',
      options,
      fingerprint,
      upload_id: null,
      conversion_id: null,
      error: null,
    };
    const upload: MockUpload = {
      upload_id: `upl_${randomBytes(6).toString('hex')}`,
      item_id: item.item_id,
      filename,
      state: 'pending',
      expected_bytes: size,
      received_bytes: 0,
      sha256: null,
      part_size: this.partSize,
      parts: new Map(),
      created_at: iso(),
    };
    item.upload_id = upload.upload_id;
    this.items.set(item.item_id, item);
    this.uploads.set(upload.upload_id, upload);
    batch.version += 1;

    return {
      item: this.itemPayload(item),
      upload_id: upload.upload_id,
      part_size: upload.part_size,
      next_part: 0,
      replayed: false,
      expires_at: iso(new Date(Date.now() + 3_600_000)),
    };
  }

  private nextPart(upload: MockUpload): number {
    let highest = -1;
    for (const number of upload.parts.keys()) highest = Math.max(highest, number);
    return highest + 1;
  }

  private uploadPayload(upload: MockUpload): Record<string, unknown> {
    const parts = [...upload.parts.values()].sort((a, b) => a.part_number - b.part_number);
    return {
      upload_id: upload.upload_id,
      item_id: upload.item_id,
      filename: upload.filename,
      state: upload.state,
      expected_bytes: upload.expected_bytes,
      received_bytes: upload.received_bytes,
      sha256: upload.sha256,
      part_size: upload.part_size,
      parts_received: parts.length,
      next_part: this.nextPart(upload),
      expires_at: iso(new Date(Date.now() + 3_600_000)),
      created_at: upload.created_at,
      updated_at: iso(),
      parts: parts.map((part) => ({
        part_number: part.part_number,
        size: part.size,
        sha256: part.sha256,
      })),
      next_cursor: null,
    };
  }

  private putPart(
    upload: MockUpload,
    partNumber: number,
    body: Buffer,
    request: IncomingMessage,
  ): Record<string, unknown> {
    if (upload.state === 'completed' || upload.state === 'aborted') {
      throw new MockApiError(409, 'UPLOAD_STATE_CONFLICT', 'Sessão encerrada.');
    }
    const maxParts = this.expectedParts(upload.expected_bytes);
    if (partNumber < 0 || partNumber >= maxParts) {
      throw new MockApiError(400, 'UPLOAD_PART_MISMATCH', 'part_number inválido.');
    }
    this.partPuts.push({ upload_id: upload.upload_id, part_number: partNumber });

    if (this.failPartsAtOrAfter !== null && partNumber >= this.failPartsAtOrAfter) {
      throw new MockApiError(500, 'INTERNAL_ERROR', 'Falha simulada ao receber a parte.');
    }

    let data = body;
    if (this.corruptParts > 0) {
      this.corruptParts -= 1;
      data = Buffer.from(body);
      if (data.length > 0) data[0] = data[0]! ^ 0xff;
    }
    const actual = sha256(data);
    const declared = request.headers['x-part-sha256'];
    if (typeof declared === 'string' && declared.toLowerCase() !== actual) {
      throw new MockApiError(422, 'UPLOAD_CHECKSUM_MISMATCH', 'O hash da parte não confere.', {
        part_number: partNumber,
        expected: declared.toLowerCase(),
        received: actual,
      });
    }
    if (data.length > upload.part_size) {
      throw new MockApiError(413, 'FILE_TOO_LARGE', 'Parte grande demais.');
    }
    const existing = upload.parts.get(partNumber);
    if (existing) {
      if (existing.sha256 !== actual) {
        throw new MockApiError(409, 'UPLOAD_PART_MISMATCH', 'Parte já confirmada com outro conteúdo.');
      }
      return {
        upload_id: upload.upload_id,
        part_number: partNumber,
        size: existing.size,
        sha256: existing.sha256,
        received_bytes: upload.received_bytes,
        parts_received: upload.parts.size,
        next_part: this.nextPart(upload),
        replayed: true,
      };
    }
    upload.parts.set(partNumber, {
      part_number: partNumber,
      size: data.length,
      sha256: actual,
      data,
    });
    upload.received_bytes += data.length;
    upload.state = 'uploading';
    return {
      upload_id: upload.upload_id,
      part_number: partNumber,
      size: data.length,
      sha256: actual,
      received_bytes: upload.received_bytes,
      parts_received: upload.parts.size,
      next_part: this.nextPart(upload),
      replayed: false,
    };
  }

  private complete(upload: MockUpload, json: Record<string, unknown>): Record<string, unknown> {
    const item = this.items.get(upload.item_id);
    if (!item) throw new MockApiError(404, 'NOT_FOUND', 'Item não encontrado.');
    const expectedSha = String(json.sha256 ?? '').toLowerCase();
    if (!/^[0-9a-f]{64}$/.test(expectedSha)) {
      throw new MockApiError(422, 'UPLOAD_CHECKSUM_MISMATCH', 'sha256 inválido.');
    }
    if (upload.state === 'completed' && item.conversion_id) {
      return {
        upload_id: upload.upload_id,
        item_id: item.item_id,
        conversion_id: item.conversion_id,
        status: 'queued',
        message: 'Upload já concluído.',
        replayed: true,
      };
    }
    const expected = this.expectedParts(upload.expected_bytes);
    if (Number(json.total_parts) !== expected) {
      throw new MockApiError(409, 'UPLOAD_INCOMPLETE', 'Número de partes divergente.', {
        expected_parts: expected,
      });
    }
    const parts: Buffer[] = [];
    for (let number = 0; number < expected; number += 1) {
      const part = upload.parts.get(number);
      if (!part) {
        throw new MockApiError(409, 'UPLOAD_INCOMPLETE', 'Partes faltando.', {
          missing_parts: [number],
        });
      }
      parts.push(part.data);
    }
    const assembled = Buffer.concat(parts);
    if (sha256(assembled) !== expectedSha) {
      throw new MockApiError(422, 'UPLOAD_CHECKSUM_MISMATCH', 'Hash do documento não confere.', {
        expected: expectedSha,
        received: sha256(assembled),
      });
    }

    const conversion: MockConversion = {
      conversion_id: `conv_${randomBytes(6).toString('hex')}`,
      item_id: item.item_id,
      status: 'completed',
      filename: item.filename,
      sha256: expectedSha,
      options: item.options,
      exports: [],
    };
    const formats = (item.options.target_formats as string[]) ?? ['xlsx'];
    const stem = item.filename.replace(/\.[^.]+$/, '');
    for (const format of formats) {
      const data = Buffer.from(`artifact:${format}:${stem}:${expectedSha.slice(0, 8)}`, 'utf8');
      conversion.exports.push({
        export_id: `exp_${randomBytes(6).toString('hex')}`,
        format,
        filename: `${stem}.${format}`,
        mime_type: format === 'xlsx' ? 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' : 'text/plain',
        sha256: sha256(data),
        size_bytes: data.length,
        publication_status: 'approved',
        data,
        created_at: iso(),
      });
    }
    this.conversions.set(conversion.conversion_id, conversion);

    upload.state = 'completed';
    upload.sha256 = expectedSha;
    upload.received_bytes = assembled.length;
    item.status = 'completed';
    item.conversion_id = conversion.conversion_id;
    item.error = null;

    return {
      upload_id: upload.upload_id,
      item_id: item.item_id,
      conversion_id: conversion.conversion_id,
      status: 'queued',
      message: 'Upload concluído.',
      replayed: false,
    };
  }
}
