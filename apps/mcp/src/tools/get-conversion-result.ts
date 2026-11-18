/**
 * `get_conversion_result` — compact result overview.
 *
 * Returns counters, the sheet list, artifact metadata and a small preview
 * (default 20 rows, max 100). It never returns file contents.
 */

import type { McpServer } from '@modelcontextprotocol/server';
import { z } from 'zod';

import { ApiClientError } from '../api-client.js';
import { clampLimit, fail, ok, type ToolContext } from './shared.js';

const InputSchema = z.object({
  conversion_id: z.string().min(1),
  sheet: z.string().optional(),
  offset: z.number().int().min(0).optional(),
  limit: z.number().int().min(1).max(100).optional(),
});

export function registerGetConversionResult(server: McpServer, ctx: ToolContext): void {
  server.registerTool(
    'get_conversion_result',
    {
      title: 'Get conversion result',
      description:
        'Return a compact summary of a finished conversion: status, quality, counters, sheets, ' +
        'artifact metadata and a small preview (default 20 rows, max 100).',
      inputSchema: InputSchema,
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true },
    },
    async (input) => {
      try {
        const result = await ctx.api.getResult(input.conversion_id);
        const limit = clampLimit(input.limit);
        const offset = input.offset ?? 0;
        const sheet = input.sheet ?? result.sheets[0]?.name;

        let preview: Record<string, unknown> | null = null;
        let previewError: { code: string; message: string } | null = null;
        try {
          const page = await ctx.api.getPreview(input.conversion_id, { sheet, offset, limit });
          preview = {
            sheet: page.sheet,
            headers: page.headers,
            rows: page.rows.map((row) => row.map((cell) => cell.display)),
            total_rows: page.total_rows,
            offset: page.offset,
            limit: page.limit,
          };
        } catch (error) {
          if (error instanceof ApiClientError && error.code === 'NOT_FOUND') {
            previewError = { code: error.code, message: error.message };
          } else {
            throw error;
          }
        }

        const exports = await ctx.api.listExports(input.conversion_id);
        const artifacts = exports.items.map((artifact) => ({
          export_id: artifact.export_id,
          format: artifact.format,
          filename: artifact.filename,
          mime_type: artifact.mime_type,
          size_bytes: artifact.size_bytes,
          sha256: artifact.sha256,
          publication_status: artifact.publication_status,
          created_at: artifact.created_at,
          download_url: artifact.download_url,
        }));

        const structured = {
          conversion_id: result.conversion_id,
          status: result.status,
          quality: result.quality,
          counts: {
            pages: result.pages,
            tables: result.tables,
            sheets: result.sheets_count,
            cells: result.cells,
            records: result.records,
            fields: result.fields,
          },
          sheets: result.sheets.map((entry) => ({
            name: entry.name,
            kind: entry.kind,
            rows: entry.rows,
            columns: entry.columns,
            table_id: entry.table_id,
          })),
          warnings: result.warnings.length,
          download_url: result.download_url,
          audit_url: result.audit_url,
          preview,
          preview_error: previewError,
          artifacts,
        };

        return ok(
          structured,
          `Conversion ${result.conversion_id}: ${result.status} (quality ${result.quality}); ` +
            `${result.pages} page(s), ${result.sheets_count} sheet(s), ${result.records} record(s), ` +
            `${artifacts.length} artifact(s). Preview: ${preview ? `${(preview.rows as unknown[]).length} row(s)` : 'unavailable'}.`,
        );
      } catch (error) {
        return fail(error);
      }
    },
  );
}
