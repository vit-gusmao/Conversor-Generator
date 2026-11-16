import type {
  Conversion,
  ConversionListItem,
  ConversionStatus,
  Paginated,
  Preview,
  ResultSummary,
} from '@pdf2x/contracts';
import { apiBlob, apiFetch, buildUrl } from '@/lib/api';

export interface CreateConversionResponse {
  conversion_id: string;
  status: ConversionStatus;
  message: string;
  links: Record<string, string>;
}

export interface ListConversionsParams {
  status?: ConversionStatus | null;
  q?: string | null;
  page?: number;
  page_size?: number;
}

function idempotencyKey(): string {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID();
    }
  } catch {
    // ignore
  }
  return `upload-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export interface UploadOptions {
  target_formats?: string[];
  text?: { newline?: 'lf' | 'crlf'; page_separator?: 'form_feed' | 'marker' | 'none' };
  csv?: { profile?: 'interoperable' | 'excel_br'; formula_policy?: 'safe' | 'raw' };
  ocr?: { enabled?: boolean; dpi?: number };
}

export async function uploadConversion(
  file: File,
  options: UploadOptions = {},
): Promise<CreateConversionResponse> {
  const form = new FormData();
  form.append('file', file, file.name);
  if (Object.keys(options).length > 0) {
    form.append('options', JSON.stringify(options));
  }
  return apiFetch<CreateConversionResponse>('/conversions', {
    method: 'POST',
    body: form,
    headers: { 'Idempotency-Key': idempotencyKey() },
  });
}

/* -------------------------------------------------------------------------- */
/* Capabilities, artifacts and text                                           */
/* -------------------------------------------------------------------------- */

export interface CapabilityInput {
  type: string | string[];
  formats?: string[];
  outputs: string[];
  multipage: boolean;
  available: boolean;
  group?: string;
  dependency?: string | null;
  notes?: string;
}

export interface ExportFormatSpec {
  format: string;
  mime_type: string;
  extension: string;
  description: string;
  available: boolean;
}

export interface Capabilities {
  inputs: CapabilityInput[];
  outputs: ExportFormatSpec[];
  ocr: { available: boolean; selected?: string | null; engines?: unknown };
  limits: {
    max_bytes: number;
    max_pages: number;
    max_image_frames?: number;
    max_image_megapixels?: number;
    max_job_megapixels?: number;
  };
}

export interface ConversionArtifact {
  export_id: string;
  format: string;
  filename: string;
  mime_type: string;
  sha256: string;
  size_bytes: number;
  warnings: Array<{ code?: string; message?: string; count?: number }>;
  options: Record<string, unknown>;
  publication_status: 'approved' | 'quarantined' | 'expired' | string;
  download_url?: string | null;
  created_at?: string | null;
}

export async function getCapabilities(): Promise<Capabilities> {
  return apiFetch<Capabilities>('/capabilities');
}

export async function listExports(id: string): Promise<{ items: ConversionArtifact[]; total: number }> {
  return apiFetch<{ items: ConversionArtifact[]; total: number }>(
    `/conversions/${encodeURIComponent(id)}/exports`,
  );
}

export async function createExport(
  id: string,
  format: 'txt' | 'csv',
  options: Record<string, unknown> = {},
): Promise<ConversionArtifact> {
  return apiFetch<ConversionArtifact>(`/conversions/${encodeURIComponent(id)}/exports`, {
    method: 'POST',
    json: { format, options },
  });
}

export async function downloadExport(id: string, exportId: string): Promise<Blob> {
  return apiBlob(
    `/conversions/${encodeURIComponent(id)}/exports/${encodeURIComponent(exportId)}/download`,
  );
}

export interface TextPage {
  page: number | null;
  offset: number;
  limit: number;
  chars: number;
  text: string;
  pages: Array<{ page_number: number; status: string; chars: number; has_text: boolean }>;
  warnings: Array<{ code?: string; message?: string; page_number?: number }>;
}

export async function getTextPage(
  id: string,
  params: { page?: number | null; offset?: number; limit?: number } = {},
): Promise<TextPage> {
  return apiFetch<TextPage>(`/conversions/${encodeURIComponent(id)}/text`, {
    query: {
      page: params.page ?? undefined,
      offset: params.offset ?? 0,
      limit: params.limit ?? 50_000,
    },
  });
}

export async function listConversions(
  params: ListConversionsParams = {},
): Promise<Paginated<ConversionListItem>> {
  return apiFetch<Paginated<ConversionListItem>>('/conversions', {
    query: {
      status: params.status ?? undefined,
      q: params.q?.trim() ? params.q.trim() : undefined,
      page: params.page ?? 1,
      page_size: params.page_size ?? 20,
    },
  });
}

export async function getConversion(id: string): Promise<Conversion> {
  return apiFetch<Conversion>(`/conversions/${encodeURIComponent(id)}`);
}

export async function getResult(id: string): Promise<ResultSummary> {
  return apiFetch<ResultSummary>(`/conversions/${encodeURIComponent(id)}/result`);
}

export async function getPreview(
  id: string,
  params: { sheet?: string | null; offset?: number; limit?: number } = {},
): Promise<Preview> {
  return apiFetch<Preview>(`/conversions/${encodeURIComponent(id)}/preview`, {
    query: {
      sheet: params.sheet ?? undefined,
      offset: params.offset ?? 0,
      limit: params.limit ?? 100,
    },
  });
}

export async function downloadExcel(id: string): Promise<Blob> {
  return apiBlob(`/conversions/${encodeURIComponent(id)}/download`);
}

export async function retryConversion(id: string): Promise<CreateConversionResponse> {
  return apiFetch<CreateConversionResponse>(`/conversions/${encodeURIComponent(id)}/retry`, {
    method: 'POST',
  });
}

export async function cancelConversion(id: string): Promise<Conversion> {
  return apiFetch<Conversion>(`/conversions/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
}

export async function deleteConversion(id: string): Promise<void> {
  await apiFetch<void>(`/conversions/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

/** SSE endpoint. `access_token` is only used by the EventSource fallback. */
export function conversionEventsUrl(
  id: string,
  options: { lastEventId?: string | null; accessToken?: string | null } = {},
): string {
  return buildUrl(`/conversions/${encodeURIComponent(id)}/events`, {
    last_event_id: options.lastEventId ?? undefined,
    access_token: options.accessToken ?? undefined,
  });
}
