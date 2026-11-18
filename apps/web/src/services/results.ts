import type { ResultOverview, SheetPage, SheetSourcePages } from '@pdf2x/contracts';

import { apiFetch } from '@/lib/api';

/**
 * Compact result endpoints.
 *
 * `result-summary` returns the same payload size for a 10-cell workbook and a
 * ten-million-cell one, and `sheets` pages the selector metadata. The legacy
 * `GET /conversions/:id/result` stays available for old clients.
 */
export async function getResultOverview(id: string): Promise<ResultOverview> {
  return apiFetch<ResultOverview>(`/conversions/${encodeURIComponent(id)}/result-summary`);
}

export async function listSheets(
  id: string,
  params: { cursor?: string | null; limit?: number; q?: string | null } = {},
): Promise<SheetPage> {
  return apiFetch<SheetPage>(`/conversions/${encodeURIComponent(id)}/sheets`, {
    query: {
      cursor: params.cursor ?? undefined,
      limit: params.limit ?? 50,
      q: params.q?.trim() ? params.q.trim() : undefined,
    },
  });
}

export async function getSheetPages(
  id: string,
  sheetId: string,
  params: { cursor?: number | null; limit?: number } = {},
): Promise<SheetSourcePages> {
  return apiFetch<SheetSourcePages>(
    `/conversions/${encodeURIComponent(id)}/sheets/${encodeURIComponent(sheetId)}/pages`,
    {
      query: {
        cursor: params.cursor ?? undefined,
        limit: params.limit ?? 200,
      },
    },
  );
}
