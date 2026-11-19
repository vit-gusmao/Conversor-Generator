import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { STALE_TIME } from '@/lib/query-keys';
import {
  archiveSource,
  createSource,
  getImport,
  listImportItems,
  listSourceRoots,
  listSources,
  startImport,
  updateSource,
  validateSource,
  type CreateSourceInput,
  type StartImportInput,
} from '@/services/sources';

export const sourceKeys = {
  roots: ['sources', 'roots'] as const,
  list: ['sources', 'list'] as const,
  detail: (id: string) => ['sources', 'detail', id] as const,
  import: (id: string) => ['imports', id] as const,
  importItems: (id: string) => ['imports', id, 'items'] as const,
};

export function useSourceRoots() {
  return useQuery({
    queryKey: sourceKeys.roots,
    queryFn: listSourceRoots,
    staleTime: STALE_TIME.settings,
  });
}

export function useSources() {
  return useQuery({
    queryKey: sourceKeys.list,
    queryFn: listSources,
    staleTime: STALE_TIME.history,
  });
}

export function useCreateSource() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: CreateSourceInput) => createSource(input),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: sourceKeys.list }),
  });
}

export function useUpdateSource() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { sourceId: string; version: number; changes: Partial<CreateSourceInput> }) =>
      updateSource(input.sourceId, input.version, input.changes),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: sourceKeys.list }),
  });
}

export function useArchiveSource() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (sourceId: string) => archiveSource(sourceId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: sourceKeys.list }),
  });
}

export function useValidateSource() {
  return useMutation({ mutationFn: (sourceId: string) => validateSource(sourceId) });
}

export function useStartImport() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { sourceId: string; body: StartImportInput; idempotencyKey: string }) =>
      startImport(input.sourceId, input.body, input.idempotencyKey),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: sourceKeys.list }),
  });
}

export function useImport(importId: string | null) {
  return useQuery({
    queryKey: sourceKeys.import(importId ?? 'none'),
    queryFn: () => getImport(importId as string),
    enabled: Boolean(importId),
    // Polling fallback: the import row is the authority for progress.
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data) return 2000;
      return ['completed', 'partial', 'failed', 'cancelled'].includes(data.status) ? false : 2000;
    },
    staleTime: 0,
  });
}

export function useImportItems(importId: string | null) {
  return useQuery({
    queryKey: sourceKeys.importItems(importId ?? 'none'),
    queryFn: () => listImportItems(importId as string),
    enabled: Boolean(importId),
    staleTime: 2_000,
  });
}
