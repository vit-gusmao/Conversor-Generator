/** Typed wrappers for the batch endpoints (`/api/v1/batches`). */

import type {
  BatchItem,
  BatchItemStatus,
  BatchSummary,
  CursorPage,
  CreateBatchRequest,
  RegisterBatchItemRequest,
  RegisterBatchItemResponse,
  SealBatchResponse,
} from '@pdf2x/contracts';
import { apiFetch, buildUrl } from '@/lib/api';

export interface ListBatchItemsParams {
  cursor?: string | null;
  limit?: number;
  status?: BatchItemStatus | null;
}

/** Extra fields the retry endpoint may return alongside the refreshed item. */
export interface RetryBatchItemResponse {
  item: BatchItem;
  upload_id?: string | null;
  part_size?: number | null;
  next_part?: number | null;
  replayed?: boolean;
  message?: string;
}

function batchPath(batchId: string): string {
  return `/batches/${encodeURIComponent(batchId)}`;
}

function itemPath(batchId: string, itemId: string): string {
  return `${batchPath(batchId)}/items/${encodeURIComponent(itemId)}`;
}

export async function createBatch(input: CreateBatchRequest): Promise<BatchSummary> {
  return apiFetch<BatchSummary>('/batches', { method: 'POST', json: input });
}

export async function getBatch(batchId: string): Promise<BatchSummary> {
  return apiFetch<BatchSummary>(batchPath(batchId));
}

export async function registerItem(
  batchId: string,
  input: RegisterBatchItemRequest,
  options: { signal?: AbortSignal } = {},
): Promise<RegisterBatchItemResponse> {
  return apiFetch<RegisterBatchItemResponse>(`${batchPath(batchId)}/items`, {
    method: 'POST',
    json: input,
    signal: options.signal,
  });
}

export async function listBatchItems(
  batchId: string,
  params: ListBatchItemsParams = {},
): Promise<CursorPage<BatchItem>> {
  return apiFetch<CursorPage<BatchItem>>(`${batchPath(batchId)}/items`, {
    query: {
      cursor: params.cursor ?? undefined,
      limit: params.limit ?? undefined,
      status: params.status ?? undefined,
    },
  });
}

export async function sealBatch(batchId: string): Promise<SealBatchResponse> {
  return apiFetch<SealBatchResponse>(`${batchPath(batchId)}/seal`, { method: 'POST' });
}

export async function cancelBatch(batchId: string): Promise<BatchSummary> {
  return apiFetch<BatchSummary>(`${batchPath(batchId)}/cancel`, { method: 'POST' });
}

export async function cancelItem(batchId: string, itemId: string): Promise<BatchItem> {
  return apiFetch<BatchItem>(`${itemPath(batchId, itemId)}/cancel`, { method: 'POST' });
}

export async function retryItem(
  batchId: string,
  itemId: string,
  scope: 'upload' | 'conversion',
): Promise<RetryBatchItemResponse> {
  return apiFetch<RetryBatchItemResponse>(`${itemPath(batchId, itemId)}/retry`, {
    method: 'POST',
    json: { scope },
  });
}

/** SSE endpoint. `access_token` is only used by the EventSource fallback. */
export function batchEventsUrl(
  batchId: string,
  options: { lastEventId?: string | null; accessToken?: string | null } = {},
): string {
  return buildUrl(`${batchPath(batchId)}/events`, {
    last_event_id: options.lastEventId ?? undefined,
    access_token: options.accessToken ?? undefined,
  });
}
