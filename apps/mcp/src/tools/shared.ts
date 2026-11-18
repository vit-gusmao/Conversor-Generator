/**
 * Shared helpers for tool modules.
 *
 * Every tool returns the SDK's structured content plus one short text summary.
 * Tool-level failures are `isError: true` and keep the API's own `status`/`code`
 * so a host can decide whether to retry or to ask the user.
 */

import type { CallToolResult } from '@modelcontextprotocol/server';

import { ApiClientError, type ApiClient } from '../api-client.js';
import type { McpConfig } from '../config.js';
import { PathPolicyError } from '../path-policy.js';
import { SubmissionStoreError, type SubmissionItemRecord, type SubmissionStore } from '../submission-store.js';
import type { TransferWorker } from '../transfer-worker.js';

export interface ToolContext {
  config: McpConfig;
  api: ApiClient;
  store: SubmissionStore;
  worker: TransferWorker;
}

export interface ToolErrorBody {
  code: string;
  message: string;
  details: Record<string, unknown>;
  status?: number;
}

export const DEFAULT_PAGE_SIZE = 20;
export const MAX_PAGE_SIZE = 100;

export function clampLimit(value: number | undefined, fallback = DEFAULT_PAGE_SIZE): number {
  if (value === undefined || !Number.isFinite(value) || value <= 0) return fallback;
  return Math.min(Math.floor(value), MAX_PAGE_SIZE);
}

export function ok(structured: Record<string, unknown>, text: string): CallToolResult {
  return {
    content: [{ type: 'text', text }],
    structuredContent: structured,
  };
}

export function toToolError(error: unknown): ToolErrorBody {
  if (error instanceof ApiClientError) {
    return {
      code: error.code,
      message: error.message,
      details: error.details,
      status: error.status === 0 ? undefined : error.status,
    };
  }
  if (error instanceof PathPolicyError || error instanceof SubmissionStoreError) {
    return {
      code: error.code,
      message: error.message,
      details: error.details,
    };
  }
  // Tool modules may build an error body directly (validation errors).
  if (
    typeof error === 'object' &&
    error !== null &&
    typeof (error as { code?: unknown }).code === 'string' &&
    typeof (error as { message?: unknown }).message === 'string'
  ) {
    const body = error as {
      code: string;
      message: string;
      details?: Record<string, unknown>;
      status?: number;
    };
    return {
      code: body.code,
      message: body.message,
      details: body.details ?? {},
      status: body.status,
    };
  }
  return {
    code: 'INTERNAL_ERROR',
    message: error instanceof Error ? error.message : String(error),
    details: {},
  };
}

export function fail(error: unknown, text?: string): CallToolResult {
  const body = toToolError(error);
  const summary =
    text ??
    `${body.code}${body.status ? ` (HTTP ${body.status})` : ''}: ${body.message}`;
  return {
    content: [{ type: 'text', text: summary }],
    structuredContent: { error: body },
    isError: true,
  };
}

/** Opaque offset cursor for submissions and batch item pages. */
export function encodeCursor(offset: number): string {
  return Buffer.from(JSON.stringify({ o: offset }), 'utf8').toString('base64url');
}

export function decodeCursor(cursor: string | null | undefined): number {
  if (!cursor) return 0;
  try {
    const parsed = JSON.parse(Buffer.from(cursor, 'base64url').toString('utf8')) as {
      o?: unknown;
    };
    return typeof parsed.o === 'number' && Number.isInteger(parsed.o) && parsed.o >= 0
      ? parsed.o
      : 0;
  } catch {
    return 0;
  }
}

export interface PageResult<T> {
  items: T[];
  next_cursor: string | null;
  total: number;
}

export function paginate<T>(
  items: readonly T[],
  cursor: string | null | undefined,
  limit: number,
): PageResult<T> {
  const offset = decodeCursor(cursor);
  const slice = items.slice(offset, offset + limit);
  const nextOffset = offset + slice.length;
  return {
    items: slice,
    next_cursor: nextOffset < items.length ? encodeCursor(nextOffset) : null,
    total: items.length,
  };
}

/** Public (compact) view of one ledger item. */
export function submissionItemView(item: SubmissionItemRecord): Record<string, unknown> {
  return {
    client_item_id: item.client_item_id,
    filename: item.filename,
    size: item.size,
    sha256: item.sha256,
    state: item.state,
    accepted: item.accepted,
    upload_id: item.upload_id,
    conversion_id: item.conversion_id,
    item_id: item.item_id,
    next_part: item.next_part,
    error: item.error,
  };
}
