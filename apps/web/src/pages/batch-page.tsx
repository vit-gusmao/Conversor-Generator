import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import type { InfiniteData } from '@tanstack/react-query';
import type { BatchEvent, BatchItem, BatchSummary, CursorPage } from '@pdf2x/contracts';
import { isTerminalItemStatus } from '@pdf2x/contracts';
import {
  ArrowRight,
  FileImage,
  FileText,
  Loader2,
  PackageCheck,
  RotateCcw,
  StopCircle,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Progress } from '@/components/ui/progress';
import { PageHeader } from '@/components/layout/page-header';
import { EmptyBlock, ErrorBlock, LoadingBlock } from '@/components/layout/states';
import {
  BatchItemStatusBadge,
  BatchStatusBadge,
  BatchSummaryPanel,
} from '@/components/batch-summary';
import {
  batchItemsKeyPrefix,
  useBatch,
  useBatchItems,
  useCancelBatch,
  useCancelBatchItem,
  useRetryBatchItem,
  useSealBatch,
} from '@/hooks/use-batch';
import { isTerminalBatchStatus, useBatchEvents } from '@/hooks/use-batch-events';
import { useEnsureSession } from '@/hooks/use-session';
import { explainError } from '@/lib/errors';
import { formatBytes, formatDateTime, formatNumber } from '@/lib/format';
import { queryKeys } from '@/lib/query-keys';
import { toast } from '@/stores/ui';

const ITEMS_RENDER_STEP = 100;

function isBatchSummary(value: unknown): value is BatchSummary {
  if (typeof value !== 'object' || value === null) return false;
  const candidate = value as Partial<BatchSummary>;
  return (
    typeof candidate.batch_id === 'string' &&
    typeof candidate.status === 'string' &&
    typeof candidate.counts === 'object' &&
    candidate.counts !== null &&
    typeof candidate.progress === 'object' &&
    candidate.progress !== null
  );
}

function isBatchItem(value: unknown): value is BatchItem {
  if (typeof value !== 'object' || value === null) return false;
  const candidate = value as Partial<BatchItem>;
  return (
    typeof candidate.item_id === 'string' &&
    typeof candidate.batch_id === 'string' &&
    typeof candidate.status === 'string'
  );
}

export function BatchPage() {
  const { id = '' } = useParams<{ id: string }>();
  useEnsureSession();
  const queryClient = useQueryClient();

  const batchQuery = useBatch(id);
  const itemsQuery = useBatchItems(id);
  const sealMutation = useSealBatch(id);
  const cancelMutation = useCancelBatch(id);
  const cancelItemMutation = useCancelBatchItem(id);
  const retryItemMutation = useRetryBatchItem(id);

  const [visibleCount, setVisibleCount] = useState(ITEMS_RENDER_STEP);

  useEffect(() => {
    setVisibleCount(ITEMS_RENDER_STEP);
  }, [id]);

  const items = useMemo(
    () => itemsQuery.data?.pages.flatMap((page: CursorPage<BatchItem>) => page.items) ?? [],
    [itemsQuery.data],
  );

  const batch = batchQuery.data;

  const applyEvent = useCallback(
    (event: BatchEvent) => {
      const detailKey = queryKeys.batches.detail(id);
      const itemsKey = batchItemsKeyPrefix(id);

      const payloadBatch = event.payload?.batch;
      if (isBatchSummary(payloadBatch)) {
        queryClient.setQueryData(detailKey, payloadBatch);
      }

      const payloadItem = event.payload?.item;
      if (isBatchItem(payloadItem)) {
        queryClient.setQueriesData<InfiniteData<CursorPage<BatchItem>, string | null>>(
          { queryKey: itemsKey },
          (data) => {
            if (!data) return data;
            return {
              ...data,
              pages: data.pages.map((page) => ({
                ...page,
                items: page.items.map((item) =>
                  item.item_id === payloadItem.item_id ? payloadItem : item,
                ),
              })),
            };
          },
        );
      }

      // The stream is a hint; the REST resources remain the source of truth.
      void queryClient.invalidateQueries({ queryKey: detailKey });
      void queryClient.invalidateQueries({ queryKey: itemsKey });
    },
    [id, queryClient],
  );

  const stream = useBatchEvents(id, {
    enabled: Boolean(id) && Boolean(batch) && !isTerminalBatchStatus(batch?.status ?? 'cancelled'),
    onEvent: applyEvent,
    onReconnect: () => {
      void batchQuery.refetch();
      void itemsQuery.refetch();
    },
  });

  if (!id) {
    return <ErrorBlock title="Lote inválido" message="O identificador do lote não foi informado." />;
  }

  if (batchQuery.isPending) {
    return (
      <div className="space-y-4">
        <LoadingBlock label="Carregando lote" />
        <LoadingBlock label="Carregando arquivos do lote" />
      </div>
    );
  }

  if (batchQuery.isError || !batch) {
    return (
      <div className="space-y-4">
        <ErrorBlock
          title="Lote não encontrado"
          message={explainError(batchQuery.error)}
          onRetry={() => void batchQuery.refetch()}
        />
        <Button asChild variant="secondary">
          <Link to="/">Voltar ao envio</Link>
        </Button>
      </div>
    );
  }

  const pendingFromCounts = batch.counts.pending_upload + batch.counts.uploading;
  const hasPendingUploads = items.some(
    (item) => item.status === 'pending_upload' || item.status === 'uploading',
  );
  const pendingUploads = Math.max(pendingFromCounts, hasPendingUploads ? 1 : 0);
  const canSeal =
    batch.status === 'accepting' && pendingUploads === 0 && batch.counts.total_items > 0;

  const sealExplanation =
    batch.status !== 'accepting'
      ? 'Este lote não aceita mais arquivos: ele já foi selado ou cancelado.'
      : pendingUploads > 0
        ? `Aguarde ${formatNumber(pendingUploads)} arquivo${
            pendingUploads === 1 ? '' : 's'
          } concluir o envio antes de selar o lote.`
        : batch.counts.total_items === 0
          ? 'Adicione arquivos antes de selar o lote.'
          : 'Ao selar, o lote começa a processar e não aceita novos arquivos.';

  const visibleItems = items.slice(0, visibleCount);
  const hiddenByCap = items.length > visibleItems.length;

  async function handleSeal() {
    if (!canSeal || sealMutation.isPending) return;
    try {
      await sealMutation.mutateAsync(undefined);
      toast({
        title: 'Lote selado',
        description: 'Os documentos já enviados entraram na fila de processamento.',
        variant: 'success',
      });
    } catch (cause) {
      toast({ title: 'Não foi possível selar o lote', description: explainError(cause), variant: 'error' });
    }
  }

  function handleShowMore() {
    const next = visibleCount + ITEMS_RENDER_STEP;
    setVisibleCount(next);
    if (next >= items.length && itemsQuery.hasNextPage && !itemsQuery.isFetchingNextPage) {
      void itemsQuery.fetchNextPage();
    }
  }

  function handleCancelItem(item: BatchItem) {
    cancelItemMutation.mutate(item.item_id, {
      onSuccess: () => {
        toast({ title: 'Item cancelado', description: item.filename, variant: 'warning' });
      },
      onError: (cause) => {
        toast({ title: 'Não foi possível cancelar', description: explainError(cause), variant: 'error' });
      },
    });
  }

  function handleRetryItem(item: BatchItem) {
    retryItemMutation.mutate(
      { itemId: item.item_id, scope: item.status === 'upload_failed' ? 'upload' : 'conversion' },
      {
        onSuccess: () => {
          toast({ title: 'Nova tentativa iniciada', description: item.filename, variant: 'info' });
        },
        onError: (cause) => {
          toast({ title: 'Não foi possível repetir', description: explainError(cause), variant: 'error' });
        },
      },
    );
  }

  function handleCancelBatch() {
    cancelMutation.mutate(undefined, {
      onSuccess: () => {
        toast({
          title: 'Cancelamento do lote solicitado',
          description: 'Os itens em processamento serão interrompidos com segurança.',
          variant: 'warning',
        });
      },
      onError: (cause) => {
        toast({ title: 'Não foi possível cancelar', description: explainError(cause), variant: 'error' });
      },
    });
  }

  return (
    <div>
      <PageHeader
        backTo={{ to: '/', label: 'Enviar mais arquivos' }}
        eyebrow="Lote de envio"
        title="Acompanhe o lote"
        description={
          <span className="flex flex-wrap items-center gap-2 text-xs text-muted-2">
            <span className="font-mono">{batch.batch_id.slice(0, 8)}…</span>
            <span>· criado em {formatDateTime(batch.created_at)}</span>
            {batch.sealed_at ? <span>· selado em {formatDateTime(batch.sealed_at)}</span> : null}
          </span>
        }
        actions={
          <>
            <BatchStatusBadge status={batch.status} showDot />
            {batch.status !== 'cancelled' && batch.status !== 'completed' && batch.status !== 'completed_with_errors' ? (
              <Button
                variant="outline"
                size="sm"
                onClick={handleCancelBatch}
                disabled={cancelMutation.isPending}
              >
                <StopCircle aria-hidden="true" />
                Cancelar lote
              </Button>
            ) : null}
            <Button
              onClick={handleSeal}
              disabled={!canSeal || sealMutation.isPending}
              aria-describedby="seal-help"
            >
              {sealMutation.isPending ? (
                <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
              ) : (
                <PackageCheck aria-hidden="true" />
              )}
              {sealMutation.isPending ? 'Selando…' : 'Selar lote'}
            </Button>
          </>
        }
      />

      <p id="seal-help" className="mb-4 text-xs text-muted-2" aria-live="polite">
        {sealExplanation}
      </p>

      <BatchSummaryPanel batch={batch} />

      <section aria-labelledby="batch-items-title" className="mt-6 space-y-3">
        <div className="flex flex-wrap items-end justify-between gap-2">
          <div>
            <h2 id="batch-items-title" className="text-lg font-semibold text-foreground">
              Arquivos do lote
            </h2>
            <p className="text-xs text-muted-2">
              {formatNumber(items.length)} carregado{items.length === 1 ? '' : 's'}
              {itemsQuery.hasNextPage ? ' · páginas adicionais disponíveis' : ''}
            </p>
          </div>
          <p className="text-xs text-muted-2" aria-live="polite">
            {stream.connection === 'open'
              ? 'Atualizações em tempo real conectadas.'
              : stream.connection === 'reconnecting'
                ? `Reconectando ao stream (tentativa ${stream.reconnectAttempts})…`
                : stream.connection === 'closed'
                  ? 'Atualizações encerradas.'
                  : stream.error ?? ''}
          </p>
        </div>

        {itemsQuery.isPending ? (
          <LoadingBlock label="Carregando arquivos do lote" />
        ) : itemsQuery.isError && items.length === 0 ? (
          <ErrorBlock
            title="Não foi possível carregar os arquivos"
            message={explainError(itemsQuery.error)}
            onRetry={() => void itemsQuery.refetch()}
          />
        ) : items.length === 0 ? (
          <EmptyBlock
            title="Nenhum arquivo no lote"
            description="Envie documentos do início para começar um novo lote."
            action={
              <Button asChild variant="secondary" size="sm">
                <Link to="/">Voltar ao envio</Link>
              </Button>
            }
          />
        ) : (
          <>
            <ul className="space-y-2" aria-label="Arquivos do lote">
              {visibleItems.map((item) => {
                const Icon = item.kind === 'image' ? FileImage : FileText;
                const pagesTotal = item.pages_total ?? 0;
                const pagesCompleted = item.pages_completed ?? 0;
                const pagePercent = pagesTotal > 0 ? (pagesCompleted / pagesTotal) * 100 : 0;
                const terminal = isTerminalItemStatus(item.status);

                return (
                  <li key={item.item_id}>
                    <Card>
                      <CardContent className="flex flex-wrap items-center gap-3 p-4 pt-4 sm:p-4">
                        <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-accent/10 text-accent">
                          <Icon className="size-5" aria-hidden="true" />
                        </span>

                        <div className="min-w-0 flex-1 space-y-1">
                          <p className="truncate text-sm font-medium text-foreground">{item.filename}</p>
                          <p className="text-xs text-muted-2">
                            {formatBytes(item.size_bytes)} · {item.kind === 'image' ? 'imagem' : 'PDF'}
                            {item.phase ? ` · ${item.phase}` : ''}
                            {item.pages_total !== null
                              ? ` · ${formatNumber(item.pages_completed ?? 0)}/${formatNumber(item.pages_total)} páginas`
                              : ''}
                          </p>
                          {pagesTotal > 0 ? (
                            <Progress
                              value={pagePercent}
                              aria-label={`Páginas de ${item.filename}: ${Math.round(pagePercent)}%`}
                            />
                          ) : null}
                          {item.error ? (
                            <p className="text-xs text-danger" aria-live="polite">
                              {item.error.message}
                            </p>
                          ) : null}
                        </div>

                        <BatchItemStatusBadge status={item.status} />

                        <div className="flex flex-wrap items-center gap-2">
                          {!terminal ? (
                            <Button
                              variant="ghost"
                              size="sm"
                              onClick={() => handleCancelItem(item)}
                              disabled={cancelItemMutation.isPending}
                              aria-label={`Cancelar ${item.filename}`}
                            >
                              <StopCircle aria-hidden="true" />
                              Cancelar
                            </Button>
                          ) : null}
                          {item.status === 'failed' ||
                          item.status === 'upload_failed' ||
                          item.status === 'cancelled' ? (
                            <Button
                              variant="secondary"
                              size="sm"
                              onClick={() => handleRetryItem(item)}
                              disabled={retryItemMutation.isPending}
                              aria-label={`Tentar novamente ${item.filename}`}
                            >
                              <RotateCcw aria-hidden="true" />
                              Tentar novamente
                            </Button>
                          ) : null}
                          {item.conversion_id ? (
                            <Button asChild variant="secondary" size="sm">
                              <Link to={`/conversions/${item.conversion_id}/result`}>
                                Abrir resultado
                                <ArrowRight aria-hidden="true" />
                              </Link>
                            </Button>
                          ) : null}
                        </div>
                      </CardContent>
                    </Card>
                  </li>
                );
              })}
            </ul>

            {hiddenByCap || itemsQuery.hasNextPage ? (
              <div className="flex justify-center">
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={handleShowMore}
                  disabled={itemsQuery.isFetchingNextPage}
                >
                  {itemsQuery.isFetchingNextPage ? 'Carregando…' : 'Mostrar mais'}
                </Button>
              </div>
            ) : null}
          </>
        )}
      </section>
    </div>
  );
}
