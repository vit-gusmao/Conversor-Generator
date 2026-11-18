/**
 * `cancel_item` — cancels one item without touching the rest of the batch.
 */

import type { McpServer } from '@modelcontextprotocol/server';
import { z } from 'zod';

import { fail, ok, type ToolContext } from './shared.js';

const InputSchema = z.object({
  batch_id: z.string().min(1),
  item_id: z.string().min(1),
});

export function registerCancelItem(server: McpServer, ctx: ToolContext): void {
  server.registerTool(
    'cancel_item',
    {
      title: 'Cancel batch item',
      description: 'Cancel one item of a batch; other items keep processing.',
      inputSchema: InputSchema,
      annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: true },
    },
    async (input) => {
      try {
        const item = await ctx.api.cancelItem(input.batch_id, input.item_id);
        return ok(
          {
            item_id: item.item_id,
            batch_id: item.batch_id,
            client_item_id: item.client_item_id,
            status: item.status,
            conversion_id: item.conversion_id,
            error: item.error,
          },
          `Item ${item.client_item_id} is now ${item.status}.`,
        );
      } catch (error) {
        return fail(error);
      }
    },
  );
}
