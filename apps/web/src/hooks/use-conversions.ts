import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  isTerminal,
  type Conversion,
  type ConversionListItem,
  type ConversionStatus,
  type Paginated,
  type Preview,
  type ResultSummary,
} from '@pdf2x/contracts';
import { STALE_TIME, queryKeys } from '@/lib/query-keys';
import { isApiError } from '@/lib/api';
import {
  cancelConversion,
  createExport,
  deleteConversion,
  downloadExcel,
  downloadExport,
  getCapabilities,
  getConversion,
  getPreview,
  getResult,
  listConversions,
  listExports,
  retryConversion,
  uploadConversion,
  type Capabilities,
  type ConversionArtifact,
  type CreateConversionResponse,
  type ListConversionsParams,
  type UploadOptions,
} from '@/services/conversions';

function shouldRetry(failureCount: number, error: unknown): boolean {
  if (isApiError(error) && error.status >= 400 && error.status < 500) return false;
  return failureCount < 2;
}

export function useConversion(id: string | null | undefined) {
  return useQuery<Conversion>({
    queryKey: queryKeys.conversions.detail(id ?? ''),
    queryFn: () => getConversion(id as string),
    enabled: Boolean(id),
    staleTime: STALE_TIME.status,
    retry: shouldRetry,
    refetchOnWindowFocus: false,
    // SSE is the primary transport, but its connection can be dropped or
    // interrupted by intermediate proxies. A slow poll while the
    // conversion is active keeps the screen truthful and stops at a terminal
    // state, so no timer survives the page.
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data) return false;
      return isTerminal(data.status) ? false : 5_000;
    },
  });
}

export interface HistoryFilters {
  status?: ConversionStatus | null;
  q?: string | null;
  page?: number;
  page_size?: number;
}

export function useConversions(filters: HistoryFilters = {}) {
  const params: ListConversionsParams = {
    status: filters.status ?? null,
    q: filters.q ?? null,
    page: filters.page ?? 1,
    page_size: filters.page_size ?? 20,
  };

  return useQuery<Paginated<ConversionListItem>>({
    queryKey: queryKeys.conversions.list(params),
    queryFn: () => listConversions(params),
    staleTime: STALE_TIME.history,
    placeholderData: (previous) => previous,
    retry: shouldRetry,
  });
}

export function useResult(id: string | null | undefined) {
  return useQuery<ResultSummary>({
    queryKey: queryKeys.result(id ?? ''),
    queryFn: () => getResult(id as string),
    enabled: Boolean(id),
    staleTime: STALE_TIME.result,
    retry: (failureCount, error) => {
      if (isApiError(error) && error.code === 'NOT_FOUND') return false;
      return shouldRetry(failureCount, error);
    },
  });
}

export function usePreview(
  id: string | null | undefined,
  params: { sheet?: string | null; offset?: number; limit?: number; runId?: string | null } = {},
  enabled = true,
) {
  const offset = params.offset ?? 0;
  const limit = params.limit ?? 100;
  return useQuery<Preview>({
    queryKey: queryKeys.preview(
      id ?? '',
      params.sheet ?? null,
      offset,
      limit,
      params.runId ?? null,
    ),
    queryFn: () => getPreview(id as string, { sheet: params.sheet ?? null, offset, limit }),
    enabled: Boolean(id) && enabled,
    staleTime: STALE_TIME.preview,
    retry: shouldRetry,
  });
}

export interface UploadInput {
  file: File;
  options?: UploadOptions;
}

export function useUploadConversion() {
  const queryClient = useQueryClient();
  return useMutation<CreateConversionResponse, unknown, UploadInput>({
    mutationFn: ({ file, options }) => uploadConversion(file, options),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.conversions.all });
    },
  });
}

export function useCapabilities() {
  return useQuery<Capabilities>({
    queryKey: ['capabilities'],
    queryFn: getCapabilities,
    staleTime: 5 * 60_000,
    retry: 1,
  });
}

export function useExports(id: string | null | undefined, enabled = true) {
  return useQuery<{ items: ConversionArtifact[]; total: number }>({
    queryKey: ['exports', id ?? ''],
    queryFn: () => listExports(id as string),
    enabled: Boolean(id) && enabled,
    staleTime: STALE_TIME.result,
    retry: shouldRetry,
  });
}

export function useCreateExport(id: string | null | undefined) {
  const queryClient = useQueryClient();
  return useMutation<ConversionArtifact, unknown, { format: 'txt' | 'csv'; options?: Record<string, unknown> }>({
    mutationFn: ({ format, options }) => createExport(id as string, format, options),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['exports', id ?? ''] });
    },
  });
}

export function useDownloadExport(id: string | null | undefined) {
  return useMutation<Blob, unknown, string>({
    mutationFn: (exportId: string) => downloadExport(id as string, exportId),
  });
}

export function useCancelConversion(id: string | null | undefined) {
  const queryClient = useQueryClient();
  return useMutation<Conversion, unknown, void>({
    mutationFn: () => cancelConversion(id as string),
    onSuccess: (conversion) => {
      queryClient.setQueryData(queryKeys.conversions.detail(id ?? ''), conversion);
      void queryClient.invalidateQueries({ queryKey: queryKeys.conversions.all });
    },
  });
}

export function useRetryConversion(id: string | null | undefined) {
  const queryClient = useQueryClient();
  return useMutation<CreateConversionResponse, unknown, void>({
    mutationFn: () => retryConversion(id as string),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.conversions.detail(id ?? '') });
      void queryClient.invalidateQueries({ queryKey: queryKeys.audit.all(id ?? '') });
    },
  });
}

export function useDeleteConversion() {
  const queryClient = useQueryClient();
  return useMutation<void, unknown, string>({
    mutationFn: (id: string) => deleteConversion(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.conversions.all });
    },
  });
}

export function useDownloadExcel() {
  return useMutation<Blob, unknown, string>({
    mutationFn: (id: string) => downloadExcel(id),
  });
}
