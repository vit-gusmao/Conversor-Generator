/**
 * Resumable upload endpoints (`/api/v1/uploads`).
 *
 * `putPart` deliberately does not use `apiFetch`: the part is a raw Blob and
 * the request needs `Content-Type: application/octet-stream` plus
 * `x-part-sha256`, which the JSON helper would not send.
 */

import type {
  CompleteUploadResponse,
  UploadPartReceipt,
  UploadSessionDetail,
  UploadState,
} from '@pdf2x/contracts';
import { ApiError, apiFetch, authHeaders, buildUrl } from '@/lib/api';

export interface AbortUploadResponse {
  upload_id: string;
  state: UploadState;
  deleted_parts: number;
}

export interface PutPartOptions {
  signal?: AbortSignal;
}

async function responseToApiError(response: Response): Promise<ApiError> {
  const status = response.status;
  const text = await response.text().catch(() => '');
  if (text) {
    try {
      const parsed: unknown = JSON.parse(text);
      if (typeof parsed === 'object' && parsed !== null && 'error' in parsed) {
        const error = (parsed as { error?: unknown }).error;
        if (typeof error === 'object' && error !== null) {
          const code = (error as { code?: unknown }).code;
          const message = (error as { message?: unknown }).message;
          const details = (error as { details?: unknown }).details;
          return new ApiError({
            code: typeof code === 'string' && code ? code : `HTTP_${status}`,
            message:
              typeof message === 'string' && message
                ? message
                : `O envio da parte falhou (HTTP ${status}).`,
            details,
            status,
          });
        }
      }
    } catch {
      // not JSON — fall through to the raw text
    }
  }
  return new ApiError({
    code: `HTTP_${status}`,
    message: text.trim() || `O envio da parte falhou (HTTP ${status}).`,
    details: null,
    status,
  });
}

/** Uploads one part. `partNumber` is 0-based, matching the batch contract. */
export async function putPart(
  uploadId: string,
  partNumber: number,
  chunk: Blob,
  sha256: string,
  options: PutPartOptions = {},
): Promise<UploadPartReceipt> {
  const headers: Record<string, string> = {
    ...authHeaders(),
    'Content-Type': 'application/octet-stream',
    'x-part-sha256': sha256,
  };

  let response: Response;
  try {
    response = await fetch(
      buildUrl(`/uploads/${encodeURIComponent(uploadId)}/parts/${partNumber}`),
      {
        method: 'PUT',
        headers,
        body: chunk,
        signal: options.signal,
      },
    );
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause;
    throw new ApiError({
      code: 'NETWORK_ERROR',
      message: 'Não foi possível enviar a parte do arquivo. Verifique sua conexão e tente novamente.',
      details: { cause: cause instanceof Error ? cause.message : String(cause) },
      status: 0,
    });
  }

  if (!response.ok) throw await responseToApiError(response);

  try {
    return (await response.json()) as UploadPartReceipt;
  } catch {
    throw new ApiError({
      code: 'INVALID_RESPONSE',
      message: 'A API respondeu com um formato inesperado ao receber a parte.',
      details: null,
      status: response.status,
    });
  }
}

export async function getUpload(uploadId: string): Promise<UploadSessionDetail> {
  return apiFetch<UploadSessionDetail>(`/uploads/${encodeURIComponent(uploadId)}`);
}

export async function completeUpload(
  uploadId: string,
  sha256: string,
  totalParts: number,
): Promise<CompleteUploadResponse> {
  return apiFetch<CompleteUploadResponse>(`/uploads/${encodeURIComponent(uploadId)}/complete`, {
    method: 'POST',
    json: { sha256, total_parts: totalParts },
  });
}

export async function abortUpload(uploadId: string): Promise<AbortUploadResponse> {
  return apiFetch<AbortUploadResponse>(`/uploads/${encodeURIComponent(uploadId)}`, {
    method: 'DELETE',
  });
}
