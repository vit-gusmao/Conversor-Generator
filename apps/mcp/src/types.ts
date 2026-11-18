/**
 * Wire types for the public `/api/v1` surface.
 *
 * These mirror `packages/contracts` (the single source of truth). They are
 * duplicated here as *types only* so this app can be installed without building
 * the contracts package first; any change to the contract must be reflected in
 * both places. Field names are the wire (snake_case) names.
 */

export const OUTPUT_FORMATS = ['xlsx', 'txt', 'csv'] as const;
export type OutputFormat = (typeof OUTPUT_FORMATS)[number];

export type BatchStatus =
  | 'accepting'
  | 'processing'
  | 'completed'
  | 'completed_with_errors'
  | 'cancelled';

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
  finished_items: number;
  pages_total: number;
  pages_attempted: number;
  pages_completed: number;
  pages_failed: number;
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
  version: number;
  links: Record<string, string>;
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
  kind: 'pdf' | 'image' | 'unknown';
  status: BatchItemStatus;
  options: Record<string, unknown>;
  available_formats: OutputFormat[];
  upload_id: string | null;
  conversion_id: string | null;
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

export interface CursorPage<T> {
  items: T[];
  next_cursor: string | null;
}

export interface RegisterBatchItemRequest {
  client_item_id: string;
  filename: string;
  size_bytes: number;
  options?: Record<string, unknown> | null;
}

export interface RegisterBatchItemResponse {
  item: BatchItem;
  upload_id: string | null;
  part_size: number;
  next_part: number;
  replayed: boolean;
  expires_at?: string | null;
}

export interface SealBatchResponse {
  batch_id: string;
  status: BatchStatus;
  sealed_at: string;
  message: string;
}

export interface UploadPartReceipt {
  upload_id: string;
  part_number: number;
  size: number;
  sha256: string;
  received_bytes: number;
  parts_received: number;
  next_part: number;
  replayed: boolean;
}

export interface UploadSessionPart {
  part_number: number;
  size: number;
  sha256: string;
}

export interface UploadSessionDetail {
  upload_id: string;
  item_id: string;
  filename: string;
  state: 'pending' | 'uploading' | 'completed' | 'failed' | 'aborted' | 'expired';
  expected_bytes: number;
  received_bytes: number;
  sha256: string | null;
  part_size: number;
  parts_received: number;
  next_part: number;
  expires_at: string;
  created_at: string;
  updated_at: string;
  parts?: UploadSessionPart[];
  next_cursor?: string | null;
}

export interface CompleteUploadResponse {
  upload_id: string;
  item_id: string;
  conversion_id: string;
  status: 'queued';
  message: string;
  replayed: boolean;
}

export interface SheetSummary {
  name: string;
  kind: string;
  rows: number;
  columns: number;
  source_pages: number[];
  table_id: string | null;
}

export interface AuditIssue {
  issue_id?: string;
  severity: string;
  code: string;
  message: string;
  page_number?: number | null;
  context?: Record<string, unknown>;
}

export interface ResultSummary {
  conversion_id: string;
  status: string;
  quality: string;
  pages: number;
  tables: number;
  sheets_count: number;
  cells: number;
  records: number;
  fields: number;
  download_url: string;
  audit_url: string;
  sheets: SheetSummary[];
  warnings: AuditIssue[];
}

export interface PreviewCell {
  value: string | number | null;
  type: string;
  display: string;
}

export interface Preview {
  sheet: string;
  headers: string[];
  rows: PreviewCell[][];
  total_rows: number;
  offset: number;
  limit: number;
}

export interface ExportArtifact {
  export_id: string;
  format: OutputFormat;
  filename: string;
  mime_type: string;
  sha256: string | null;
  size_bytes: number;
  warnings: unknown[];
  options: Record<string, unknown>;
  publication_status: 'approved' | 'quarantined' | 'expired' | 'pending';
  created_at: string | null;
  download_url: string | null;
}

export interface ExportList {
  items: ExportArtifact[];
  total: number;
}

export type RetryScope = 'upload' | 'conversion';

export interface RetryItemResponse {
  item: BatchItem;
  upload_id?: string;
  next_part?: number;
  part_size?: number;
  conversion?: unknown;
}
