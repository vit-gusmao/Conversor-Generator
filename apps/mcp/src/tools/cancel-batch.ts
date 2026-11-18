/**
 * `cancel_batch` — requests cancellation of the batch and its active items.
 */

import type { McpServer } from '@modelcontextprotocol/server';
import { z } from 'zod';

import { fail, ok, type ToolContext } from './shared.js';

const InputSchema = z.object({
  batch_id: z.string().min(1),
});

export function registerCancelBatch(server: McpServer, ctx: ToolContext): void {
  server.registerTool(
    'cancel_batch',
    {
      title: 'Cancel batch',
      description:
        'Request cancellation of a batch. Active items are cancelled; artifacts already published ' +
        'are not removed.',
      inputSchema: InputSchema,
      annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: true },
    },
    async (input) => {
      try {
        const batch = await ctx.api.cancelBatch(input.batch_id);
        return ok(
          {
            batch_id: batch.batch_id,
            status: batch.status,
            counts: batch.counts,
            cancel_requested_at: batch.cancel_requested_at,
            version: batch.version,
          },
          `Batch ${batch.batch_id} cancel requested; status ${batch.status}.`,
        );
      } catch (error) {
        return fail(error);
      }
    },
  );
}
