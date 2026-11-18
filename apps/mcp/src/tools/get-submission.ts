/**
 * `get_submission` — per-item transfer state from the local ledger.
 *
 * Reads only local durable state, so it answers while the API is unreachable
 * and never re-uploads anything.
 */

import type { McpServer } from '@modelcontextprotocol/server';
import { z } from 'zod';

import {
  clampLimit,
  fail,
  ok,
  paginate,
  submissionItemView,
  type ToolContext,
} from './shared.js';

const InputSchema = z.object({
  submission_id: z.string().min(1),
  cursor: z.string().optional(),
  limit: z.number().int().min(1).max(100).optional(),
});

function summarizeCounts(items: ReadonlyArray<{ state: string; accepted: boolean }>) {
  return {
    total: items.length,
    rejected: items.filter((item) => !item.accepted).length,
    pending: items.filter((item) => item.accepted && item.state === 'pending').length,
    uploading: items.filter((item) => item.accepted && item.state === 'uploading').length,
    completed: items.filter((item) => item.state === 'completed').length,
    failed: items.filter((item) => item.state === 'failed').length,
    cancelled: items.filter((item) => item.state === 'cancelled').length,
  };
}

export function registerGetSubmission(server: McpServer, ctx: ToolContext): void {
  server.registerTool(
    'get_submission',
    {
      title: 'Get submission status',
      description:
        'Return the local transfer state of a submission: per-item state, hashes, upload and ' +
        'conversion ids, and the last error. Paged (default 20, max 100).',
      inputSchema: InputSchema,
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true },
    },
    async (input) => {
      try {
        const record = ctx.store.get(input.submission_id);
        if (!record) {
          return fail({
            code: 'NOT_FOUND',
            message: `Unknown submission: ${input.submission_id}`,
            details: { submission_id: input.submission_id },
          });
        }
        const page = paginate(record.items, input.cursor, clampLimit(input.limit));
        const counts = summarizeCounts(record.items);
        const structured = {
          submission_id: record.submission_id,
          batch_id: record.batch_id,
          status: record.status,
          auto_seal: record.auto_seal,
          seal_error: record.seal_error,
          created_at: record.created_at,
          updated_at: record.updated_at,
          counts,
          next_cursor: page.next_cursor,
          items: page.items.map(submissionItemView),
        };
        return ok(
          structured,
          `Submission ${record.submission_id}: ${record.status}; ` +
            `${counts.completed} completed, ${counts.uploading} uploading, ` +
            `${counts.pending} pending, ${counts.failed} failed, ${counts.rejected} rejected.`,
        );
      } catch (error) {
        return fail(error);
      }
    },
  );
}
