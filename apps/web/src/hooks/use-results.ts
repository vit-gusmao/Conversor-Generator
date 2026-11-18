import { useQuery } from '@tanstack/react-query';
import type { ResultOverview, SheetPage, SheetSourcePages } from '@pdf2x/contracts';

import { isApiError } from '@/lib/api';
import { STALE_TIME, queryKeys } from '@/lib/query-keys';
import { getResultOverview, getSheetPages, listSheets } from '@/services/results';

function shouldRetry(failureCount: number, error: unknown): boolean {
  if (isApiError(error) && error.status >= 400 && error.status < 500) return false;
  return failureCount < 2;
}

/** Compact result payload: the same shape for any workbook size. */
export function useResultOverview(id: string | null | undefined) {
  return useQuery<ResultOverview>({
    queryKey: queryKeys.resultOverview(id ?? ''),
    queryFn: () => getResultOverview(id as string),
    enabled: Boolean(id),
    staleTime: STALE_TIME.result,
    retry: shouldRetry,
  });
}

/** Paginated sheet selector metadata (search by name, never a full list). */
export function useSheets(
  id: string | null | undefined,
  filters: { q?: string | null; cursor?: string | null; limit?: number } = {},
) {
  const params = {
    q: filters.q?.trim() ? filters.q.trim() : null,
    cursor: filters.cursor ?? null,
    limit: filters.limit ?? 50,
  };
  return useQuery<SheetPage>({
    queryKey: queryKeys.sheets(id ?? '', params),
    queryFn: () => listSheets(id as string, params),
    enabled: Boolean(id),
    staleTime: STALE_TIME.result,
    retry: shouldRetry,
  });
}

/** Per-page provenance of one sheet, paginated. */
export function useSheetPages(
  id: string | null | undefined,
  sheetId: string | null | undefined,
  cursor: number | null = null,
  enabled = true,
) {
  return useQuery<SheetSourcePages>({
    queryKey: queryKeys.sheetPages(id ?? '', sheetId ?? '', cursor),
    queryFn: () => getSheetPages(id as string, sheetId as string, { cursor }),
    enabled: Boolean(id) && Boolean(sheetId) && enabled,
    staleTime: STALE_TIME.result,
    retry: shouldRetry,
  });
}
