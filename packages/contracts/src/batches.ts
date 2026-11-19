/**
 * Batch and item contracts.
 * ============================================================================
 * A batch is a durable container for an unbounded number of documents. Each
 * item is one logical upload that becomes exactly one conversion; a batch never
 * merges documents into a single spreadsheet.
 */

import type { ConversionOptionsV2, NormalizedConversionOptions, OutputFormat } from './options';

/** Lifecycle of the container itself. */
export const BATCH_STATUSES = [
  'accepting',
  'processing',
  'completed',
  'completed_with_errors',
  'cancelled',
] as const;
export type BatchStatus = (typeof BATCH_STATUSES)[number];

/**
 * Item lifecycle. `processing` groups the finer-grained conversion statuses
 * (`inspecting`, `extracting`, …); `upload_failed` is recoverable and keeps the
 * same upload identity.
 */
export const BATCH_ITEM_STATUSES = [
  'pending_upload',
  'uploading',
  'queued',
  'processing',
  'completed',
  'completed_with_warnings',
  'failed',
  'upload_failed',
  'cancelled',
] as const;
export type BatchItemStatus = (typeof BATCH_ITEM_STATUSES)[number];

export const TERMINAL_ITEM_STATUSES: readonly BatchItemStatus[] = [
  'completed',
  'completed_with_warnings',
  'failed',
  'cancelled',
];

export function isTerminalItemStatus(status: BatchItemStatus): boolean {
  return TERMINAL_ITEM_STATUSES.includes(status);
}

/** Opaque cursor page. `next_cursor` is `null` on the last page. */
export interface CursorPage<T> {
  items: T[];
  next_cursor: string | null;
}

export interface BatchCounts {
  total_items: number;
  pending_upload: number;
  uploading: number;
  queued: number;
  processing: number;
  completed: number;
  with_warnings: number;
  failed: number;
  cancelled: number;
}

export interface BatchProgress {
  /** Items whose conversion reached a terminal state, regardless of outcome. */
  finished_items: number;
  /** Pages accounted for across the batch. */
  pages_total: number;
  pages_attempted: number;
  pages_completed: number;
  pages_failed: number;
}

export interface BatchLinks {
  self: string;
  items: string;
  events: string;
  seal: string;
  cancel: string;
}

export interface BatchSummary {
  batch_id: string;
  status: BatchStatus;
  client_batch_key: string;
  counts: BatchCounts;
  progress: BatchProgress;
  created_at: string;
  sealed_at: string | null;
  cancel_requested_at: string | null;
  /** Optimistic-concurrency version, incremented on every state change. */
  version: number;
  links: BatchLinks;
}

export interface BatchItemError {
  code: string;
  message: string;
  details?: Record<string, unknown> | null;
}

export interface BatchItem {
  item_id: string;
  batch_id: string;
  client_item_id: string;
  ordinal: number;
  filename: string;
  size_bytes: number;
  /** Declared kind; the server re-detects the real content before accepting. */
  kind: 'pdf' | 'image' | 'spreadsheet' | 'unknown';
  status: BatchItemStatus;
  options: NormalizedConversionOptions;
  available_formats: OutputFormat[];
  upload_id: string | null;
  conversion_id: string | null;
  /** Per-document counters, mirrored from the conversion run when available. */
  pages_total: number | null;
  pages_attempted: number | null;
  pages_completed: number | null;
  pages_failed: number | null;
  phase: string | null;
  error: BatchItemError | null;
  created_at: string;
  updated_at: string;
  links: Record<string, string>;
}

export interface BatchEvent {
  batch_id: string;
  /** Monotonic per batch; doubles as the SSE `id:` field. */
  sequence: number;
  type: 'batch' | 'item' | 'completed' | 'failed' | 'heartbeat' | 'reset';
  item_id: string | null;
  status: BatchStatus;
  item_status: BatchItemStatus | null;
  message: string;
  payload: Record<string, unknown>;
  at: string;
}

export interface CreateBatchRequest {
  client_batch_key: string;
}

export interface SealBatchResponse {
  batch_id: string;
  status: BatchStatus;
  sealed_at: string;
  message: string;
}

export interface RegisterBatchItemRequest {
  client_item_id: string;
  filename: string;
  size_bytes: number;
  options?: Partial<ConversionOptionsV2> | Record<string, unknown> | null;
}

export interface RegisterBatchItemResponse {
  item: BatchItem;
  /** Upload session to fill before `complete`. */
  upload_id: string;
  /** Part size the client should use, in bytes. */
  part_size: number;
  /** Byte offset the client must resume from. */
  next_part: number;
  /** True when this call replayed an earlier registration. */
  replayed: boolean;
}

export interface BatchItemListQuery {
  cursor?: string | null;
  limit?: number;
  status?: BatchItemStatus | null;
}

/** Defaults the plan fixes for list endpoints (not product ceilings). */
export const BATCH_LIST_DEFAULT_LIMIT = 50;
export const BATCH_LIST_MAX_LIMIT = 200;
