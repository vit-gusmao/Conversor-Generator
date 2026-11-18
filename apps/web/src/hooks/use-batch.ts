/** react-query bindings for the batch endpoints. */

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { InfiniteData } from '@tanstack/react-query';
import type { BatchItem, BatchItemStatus, BatchSummary, CursorPage, SealBatchResponse } from '@pdf2x/contracts';
import { STALE_TIME, queryKeys } from '@/lib/query-keys';
import { isApiError } from '@/lib/api';
import {
  cancelBatch,
  cancelItem,
  createBatch,
  getBatch,
  listBatchItems,
  retryItem,
  sealBatch,
  type ListBatchItemsParams,
  type RetryBatchItemResponse,
} from '@/services/batches';

function shouldRetry(failureCount: number, error: unknown): boolean {
  if (isApiError(error) && error.status >= 400 && error.status < 500) return false;
  return failureCount < 2;
}

/** The item-list query key prefix, for cache updates across filters. */
export function batchItemsKeyPrefix(batchId: string): readonly unknown[] {
  return ['batches', batchId, 'items'] as const;
}

export function useBatch(id: string | null | undefined) {
  return useQuery<BatchSummary>({
    queryKey: queryKeys.batches.detail(id ?? ''),
    queryFn: () => getBatch(id as string),
    enabled: Boolean(id),
    staleTime: STALE_TIME.status,
    retry: shouldRetry,
    refetchOnWindowFocus: false,
  });
}

export interface BatchItemsFilters {
  status?: BatchItemStatus | null;
  limit?: number;
}

export const BATCH_ITEMS_PAGE_SIZE = 50;

export function useBatchItems(id: string | null | undefined, filters: BatchItemsFilters = {}) {
  const limit = filters.limit ?? BATCH_ITEMS_PAGE_SIZE;
  const status = filters.status ?? null;

  return useInfiniteQuery<
    CursorPage<BatchItem>,
    unknown,
    InfiniteData<CursorPage<BatchItem>, string | null>,
    readonly unknown[],
    string | null
  >({
    queryKey: queryKeys.batches.items(id ?? '', { status, limit }),
    queryFn: ({ pageParam }) =>
      listBatchItems(id as string, {
        cursor: pageParam,
        limit,
        status,
      } satisfies ListBatchItemsParams),
    initialPageParam: null,
    getNextPageParam: (lastPage) => lastPage.next_cursor,
    enabled: Boolean(id),
    staleTime: STALE_TIME.status,
    retry: shouldRetry,
    refetchOnWindowFocus: false,
  });
}

export function useCreateBatch() {
  const queryClient = useQueryClient();
  return useMutation<BatchSummary, unknown, string>({
    mutationFn: (clientBatchKey: string) => createBatch({ client_batch_key: clientBatchKey }),
    onSuccess: (batch) => {
      queryClient.setQueryData(queryKeys.batches.detail(batch.batch_id), batch);
      void queryClient.invalidateQueries({ queryKey: queryKeys.batches.all });
    },
  });
}

export function useSealBatch(id: string | null | undefined) {
  const queryClient = useQueryClient();
  return useMutation<SealBatchResponse, unknown, void>({
    mutationFn: () => sealBatch(id as string),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.batches.detail(id ?? '') });
      void queryClient.invalidateQueries({ queryKey: batchItemsKeyPrefix(id ?? '') });
    },
  });
}

export function useCancelBatch(id: string | null | undefined) {
  const queryClient = useQueryClient();
  return useMutation<BatchSummary, unknown, void>({
    mutationFn: () => cancelBatch(id as string),
    onSuccess: (batch) => {
      queryClient.setQueryData(queryKeys.batches.detail(id ?? ''), batch);
      void queryClient.invalidateQueries({ queryKey: batchItemsKeyPrefix(id ?? '') });
    },
  });
}

export function useCancelBatchItem(id: string | null | undefined) {
  const queryClient = useQueryClient();
  return useMutation<BatchItem, unknown, string>({
    mutationFn: (itemId: string) => cancelItem(id as string, itemId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.batches.detail(id ?? '') });
      void queryClient.invalidateQueries({ queryKey: batchItemsKeyPrefix(id ?? '') });
    },
  });
}

export interface RetryBatchItemInput {
  itemId: string;
  scope?: 'upload' | 'conversion';
}

export function useRetryBatchItem(id: string | null | undefined) {
  const queryClient = useQueryClient();
  return useMutation<RetryBatchItemResponse, unknown, RetryBatchItemInput>({
    mutationFn: ({ itemId, scope }) => retryItem(id as string, itemId, scope ?? 'upload'),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.batches.detail(id ?? '') });
      void queryClient.invalidateQueries({ queryKey: batchItemsKeyPrefix(id ?? '') });
    },
  });
}
