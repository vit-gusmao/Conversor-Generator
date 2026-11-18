/**
 * `get_batch` — server-side batch counts and page progress.
 */

import type { McpServer } from '@modelcontextprotocol/server';
import { z } from 'zod';

import { fail, ok, type ToolContext } from './shared.js';

const InputSchema = z.object({
  batch_id: z.string().min(1),
});

export function registerGetBatch(server: McpServer, ctx: ToolContext): void {
  server.registerTool(
    'get_batch',
    {
      title: 'Get batch',
      description:
        'Fetch a batch from the API: lifecycle status, per-status item counts and page progress.',
      inputSchema: InputSchema,
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true },
    },
    async (input) => {
      try {
        const batch = await ctx.api.getBatch(input.batch_id);
        const total = batch.counts.total_items;
        const percent = total === 0 ? 0 : Math.round((batch.progress.finished_items / total) * 100);
        const structured = {
          batch_id: batch.batch_id,
          status: batch.status,
          client_batch_key: batch.client_batch_key,
          counts: batch.counts,
          progress: batch.progress,
          percent,
          sealed_at: batch.sealed_at,
          cancel_requested_at: batch.cancel_requested_at,
          version: batch.version,
          created_at: batch.created_at,
        };
        return ok(
          structured,
          `Batch ${batch.batch_id}: ${batch.status}; ${batch.progress.finished_items}/${total} items finished (${percent}%), ` +
            `${batch.counts.failed} failed, ${batch.counts.cancelled} cancelled.`,
        );
      } catch (error) {
        return fail(error);
      }
    },
  );
}
