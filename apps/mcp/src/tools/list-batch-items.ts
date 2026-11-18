/**
 * `list_batch_items` — server-side item page for a batch.
 */

import type { McpServer } from '@modelcontextprotocol/server';
import { z } from 'zod';

import { BATCH_ITEM_STATUSES } from '../types.js';
import { clampLimit, fail, ok, type ToolContext } from './shared.js';

const InputSchema = z.object({
  batch_id: z.string().min(1),
  cursor: z.string().optional(),
  limit: z.number().int().min(1).max(100).optional(),
  status: z.enum(BATCH_ITEM_STATUSES).optional(),
});

export function registerListBatchItems(server: McpServer, ctx: ToolContext): void {
  server.registerTool(
    'list_batch_items',
    {
      title: 'List batch items',
      description:
        'List the items of a batch with their conversion status (paged; default 20, max 100). ' +
        'Optionally filter by item status.',
      inputSchema: InputSchema,
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true },
    },
    async (input) => {
      try {
        const limit = clampLimit(input.limit);
        const page = await ctx.api.listItems(input.batch_id, {
          cursor: input.cursor ?? null,
          limit,
          status: input.status ?? null,
        });
        const items = page.items.map((item) => ({
          item_id: item.item_id,
          client_item_id: item.client_item_id,
          ordinal: item.ordinal,
          filename: item.filename,
          size_bytes: item.size_bytes,
          kind: item.kind,
          status: item.status,
          conversion_id: item.conversion_id,
          available_formats: item.available_formats,
          pages_total: item.pages_total,
          pages_completed: item.pages_completed,
          pages_failed: item.pages_failed,
          error: item.error,
          updated_at: item.updated_at,
        }));
        return ok(
          {
            batch_id: input.batch_id,
            items,
            count: items.length,
            next_cursor: page.next_cursor,
          },
          `Batch ${input.batch_id}: ${items.length} item(s) on this page.`,
        );
      } catch (error) {
        return fail(error);
      }
    },
  );
}
