import { useInfiniteQuery, useQueries, useQuery } from '@tanstack/react-query';
import type { AuditEvent, AuditIssue, AuditSummary, CellEvidence, PageAudit } from '@pdf2x/contracts';
import { STALE_TIME, queryKeys } from '@/lib/query-keys';
import { isApiError } from '@/lib/api';
import {
  getAuditEvents,
  getAuditIssues,
  getAuditSummary,
  getCellEvidence,
  getPageAudit,
  type AuditEventFilters,
  type AuditEventsPage,
  type AuditIssuesPage,
} from '@/services/audit';

function shouldRetry(failureCount: number, error: unknown): boolean {
  if (isApiError(error) && error.status >= 400 && error.status < 500) return false;
  return failureCount < 2;
}

export function useAuditSummary(id: string | null | undefined, enabled = true) {
  return useQuery<AuditSummary>({
    queryKey: queryKeys.audit.summary(id ?? ''),
    queryFn: () => getAuditSummary(id as string),
    enabled: Boolean(id) && enabled,
    staleTime: STALE_TIME.audit,
    retry: shouldRetry,
  });
}

export function useAuditIssues(id: string | null | undefined, enabled = true) {
  return useQuery<AuditIssuesPage>({
    queryKey: queryKeys.audit.issues(id ?? ''),
    queryFn: () => getAuditIssues(id as string),
    enabled: Boolean(id) && enabled,
    staleTime: STALE_TIME.audit,
    retry: shouldRetry,
  });
}

export function useAuditEvents(
  id: string | null | undefined,
  filters: AuditEventFilters = {},
  enabled = true,
) {
  const limit = filters.limit ?? 50;
  return useInfiniteQuery<AuditEventsPage>({
    queryKey: queryKeys.audit.events(id ?? '', { ...filters, limit }),
    queryFn: ({ pageParam }) =>
      getAuditEvents(id as string, {
        ...filters,
        limit,
        since_sequence: typeof pageParam === 'number' ? pageParam : filters.since_sequence ?? null,
      }),
    initialPageParam: null as number | null,
    getNextPageParam: (lastPage) => {
      if (lastPage.items.length < limit) return undefined;
      return lastPage.next_sequence ?? undefined;
    },
    enabled: Boolean(id) && enabled,
    staleTime: STALE_TIME.audit,
    retry: shouldRetry,
  });
}

export function usePageAudit(id: string | null | undefined, page: number, enabled = true) {
  return useQuery<PageAudit>({
    queryKey: queryKeys.audit.page(id ?? '', page),
    queryFn: () => getPageAudit(id as string, page),
    enabled: Boolean(id) && enabled && page > 0,
    staleTime: STALE_TIME.audit,
    retry: shouldRetry,
  });
}

/** Page audits for a bounded window of pages (one query per page). */
export function usePageAudits(id: string | null | undefined, pageCount: number, enabled = true) {
  const pages = Array.from(
    { length: Math.max(0, Math.min(pageCount, 25)) },
    (_value, index) => index + 1,
  );

  return useQueries({
    queries: pages.map((page) => ({
      queryKey: queryKeys.audit.page(id ?? '', page),
      queryFn: () => getPageAudit(id as string, page),
      enabled: Boolean(id) && enabled,
      staleTime: STALE_TIME.audit,
      retry: shouldRetry,
    })),
  });
}

export function useCellEvidence(
  id: string | null | undefined,
  filters: { sheet?: string | null; cell?: string | null },
  enabled = true,
) {
  return useQuery<CellEvidence[]>({
    queryKey: queryKeys.audit.cells(id ?? '', filters),
    queryFn: () =>
      getCellEvidence(id as string, {
        sheet: filters.sheet ?? null,
        cell: filters.cell ?? null,
        limit: 50,
      }),
    enabled: Boolean(id) && enabled && Boolean(filters.sheet || filters.cell),
    staleTime: STALE_TIME.audit,
    retry: shouldRetry,
  });
}

/**
 * Aggregated view over page audits for audit tabs that need every page
 * (fields, tables, extracted data). Bounded by `usePageAudits` (25 pages).
 */
export function useAggregatedPageAudits(
  id: string | null | undefined,
  pageCount: number,
  enabled = true,
) {
  const results = usePageAudits(id, pageCount, enabled);
  const data: PageAudit[] = results.flatMap((result) => (result.data ? [result.data] : []));
  const pending = results.filter((result) => result.isPending);
  const failed = results.filter((result) => result.isError);
  return {
    data,
    isLoading: pending.length > 0 && data.length === 0,
    isFetching: results.some((result) => result.isFetching),
    error: failed[0]?.error ?? null,
    refetch: () => {
      for (const result of results) void result.refetch();
    },
    pageCount: Math.max(0, Math.min(pageCount, 25)),
    truncated: pageCount > 25,
  };
}

export type { AuditEvent, AuditIssue };
