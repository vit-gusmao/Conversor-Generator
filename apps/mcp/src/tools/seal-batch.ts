/**
 * `seal_batch` — closes a batch to new items.
 *
 * A 409 `BATCH_HAS_PENDING_UPLOADS` is reported as a tool error, never
 * swallowed: it means another upload must finish (or be cancelled) first.
 */

import type { McpServer } from '@modelcontextprotocol/server';
import { z } from 'zod';

import { fail, ok, type ToolContext } from './shared.js';

const InputSchema = z.object({
  batch_id: z.string().min(1),
});

export function registerSealBatch(server: McpServer, ctx: ToolContext): void {
  server.registerTool(
    'seal_batch',
    {
      title: 'Seal batch',
      description:
        'Seal a batch so it accepts no new items. Fails with BATCH_HAS_PENDING_UPLOADS when the ' +
        'batch still has uploads in flight.',
      inputSchema: InputSchema,
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true },
    },
    async (input) => {
      try {
        const sealed = await ctx.api.sealBatch(input.batch_id);
        return ok(
          {
            batch_id: sealed.batch_id,
            status: sealed.status,
            sealed_at: sealed.sealed_at,
            message: sealed.message,
          },
          `Batch ${sealed.batch_id} sealed (status ${sealed.status}).`,
        );
      } catch (error) {
        return fail(error);
      }
    },
  );
}
