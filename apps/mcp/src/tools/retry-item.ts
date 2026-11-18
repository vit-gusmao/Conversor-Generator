/**
 * `retry_item` — resumes an upload session or retries a conversion.
 *
 * For `scope: "upload"` the server keeps the same upload identity and returns
 * `next_part`; when that item belongs to a submission recorded in the local
 * ledger, the transfer worker is restarted so the adapter finishes the upload.
 */

import type { McpServer } from '@modelcontextprotocol/server';
import { z } from 'zod';

import { fail, ok, type ToolContext } from './shared.js';

const InputSchema = z.object({
  batch_id: z.string().min(1),
  item_id: z.string().min(1),
  scope: z.enum(['upload', 'conversion']).optional(),
});

export function registerRetryItem(server: McpServer, ctx: ToolContext): void {
  server.registerTool(
    'retry_item',
    {
      title: 'Retry batch item',
      description:
        'Retry one item. scope="upload" resumes the resumable upload from the server next_part; ' +
        'scope="conversion" re-runs a terminal conversion. When omitted, the scope follows the ' +
        'item state: an item without a conversion resumes its upload, otherwise the conversion ' +
        'is retried.',
      inputSchema: InputSchema,
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true },
    },
    async (input) => {
      try {
        const scope = await resolveScope(ctx, input);
        const response = await ctx.api.retryItem(input.batch_id, input.item_id, scope);

        let resumed: string[] = [];
        if (scope === 'upload') {
          resumed = ctx.store
            .list()
            .filter(
              (record) =>
                record.batch_id === input.batch_id &&
                record.items.some(
                  (item) =>
                    item.state !== 'completed' &&
                    (item.item_id === response.item.item_id ||
                      item.client_item_id === response.item.client_item_id),
                ),
            )
            .map((record) => record.submission_id);
          for (const submissionId of resumed) {
            void ctx.worker.startSubmission(submissionId);
          }
        }

        return ok(
          {
            batch_id: input.batch_id,
            item_id: response.item.item_id,
            client_item_id: response.item.client_item_id,
            status: response.item.status,
            scope,
            upload_id: response.upload_id ?? response.item.upload_id,
            next_part: response.next_part ?? null,
            part_size: response.part_size ?? null,
            conversion_id: response.item.conversion_id,
            resumed_submissions: resumed,
          },
          `Item ${response.item.client_item_id} retry accepted (${scope}); status ${response.item.status}.`,
        );
      } catch (error) {
        return fail(error);
      }
    },
  );
}

/**
 * Picks the scope the caller most likely means, without guessing blindly.
 *
 * An item that never became a conversion can only resume its upload; an item
 * with a conversion (terminal, therefore retryable) is retried as a conversion.
 * An explicit `scope` always wins.
 */
async function resolveScope(
  ctx: ToolContext,
  input: { batch_id: string; item_id: string; scope?: 'upload' | 'conversion' },
): Promise<'upload' | 'conversion'> {
  if (input.scope) return input.scope;
  const listing = await ctx.api.listItems(input.batch_id, { limit: 200 });
  let cursor = listing.next_cursor;
  let items = listing.items;
  for (;;) {
    const match = items.find((item) => item.item_id === input.item_id);
    if (match) return match.conversion_id ? 'conversion' : 'upload';
    if (!cursor) break;
    const page = await ctx.api.listItems(input.batch_id, { limit: 200, cursor });
    items = page.items;
    cursor = page.next_cursor;
  }
  // Unknown item: let the API answer with its own error for the upload path.
  return 'upload';
}
