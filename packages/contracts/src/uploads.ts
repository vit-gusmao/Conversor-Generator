/**
 * Resumable upload contracts.
 * ============================================================================
 * A document is transferred as independent parts (8 MiB by default). The part
 * size is a transport parameter, not a document ceiling: a 10,000-page PDF is
 * uploaded as many parts and the session never rejects it for total size.
 */

export const UPLOAD_STATES = [
  'pending',
  'uploading',
  'completed',
  'failed',
  'aborted',
  'expired',
] as const;
export type UploadState = (typeof UPLOAD_STATES)[number];

/** 8 MiB — the default part size. A transport limit, not a document limit. */
export const DEFAULT_UPLOAD_PART_BYTES = 8 * 1024 * 1024;

/** Minimum part size; the last part may be smaller. */
export const MIN_UPLOAD_PART_BYTES = 64 * 1024;

export interface UploadPart {
  part_number: number;
  size: number;
  sha256: string;
}

export interface UploadSession {
  upload_id: string;
  item_id: string;
  filename: string;
  state: UploadState;
  expected_bytes: number;
  received_bytes: number;
  /** SHA-256 of the finished document; `null` until `complete` verifies it. */
  sha256: string | null;
  part_size: number;
  parts_received: number;
  /** 0-based: the next part number the client should send. */
  next_part: number;
  expires_at: string;
  created_at: string;
  updated_at: string;
}

export interface UploadSessionDetail extends UploadSession {
  /** Present only for the first page of parts; use `GET .../parts` for more. */
  parts: UploadPart[];
  next_cursor: string | null;
}

export interface UploadPartReceipt {
  upload_id: string;
  part_number: number;
  size: number;
  sha256: string;
  received_bytes: number;
  parts_received: number;
  next_part: number;
  /** True when an identical retransmission was detected and not stored twice. */
  replayed: boolean;
}

export interface CompleteUploadRequest {
  /** SHA-256 of the whole document; verified in streaming mode. */
  sha256: string;
  total_parts: number;
}

export interface CompleteUploadResponse {
  upload_id: string;
  item_id: string;
  conversion_id: string;
  status: 'queued';
  message: string;
  replayed: boolean;
}

export interface UploadPartListQuery {
  cursor?: string | null;
  limit?: number;
}

export const UPLOAD_PART_LIST_DEFAULT_LIMIT = 100;
export const UPLOAD_PART_LIST_MAX_LIMIT = 1000;
