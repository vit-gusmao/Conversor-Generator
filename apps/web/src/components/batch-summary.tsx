/** Compact roll-up of one batch plus its status vocabulary. */

import type { BatchItemStatus, BatchStatus, BatchSummary } from '@pdf2x/contracts';
import { Badge } from '@/components/ui/badge';
import type { BadgeProps } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { Progress } from '@/components/ui/progress';
import { formatNumber } from '@/lib/format';
import { cn } from '@/lib/utils';

export const BATCH_STATUS_LABELS: Record<BatchStatus, string> = {
  accepting: 'Aceitando arquivos',
  processing: 'Processando',
  completed: 'Concluído',
  completed_with_errors: 'Concluído com erros',
  cancelled: 'Cancelado',
};

const BATCH_STATUS_VARIANTS: Record<BatchStatus, NonNullable<BadgeProps['variant']>> = {
  accepting: 'info',
  processing: 'info',
  completed: 'success',
  completed_with_errors: 'warning',
  cancelled: 'muted',
};

const LIVE_BATCH_STATUSES: readonly BatchStatus[] = ['accepting', 'processing'];

export const BATCH_ITEM_STATUS_LABELS: Record<BatchItemStatus, string> = {
  pending_upload: 'Aguardando envio',
  uploading: 'Enviando',
  queued: 'Na fila',
  processing: 'Processando',
  completed: 'Concluído',
  completed_with_warnings: 'Concluído com avisos',
  failed: 'Falhou',
  upload_failed: 'Falha no envio',
  cancelled: 'Cancelado',
};

const BATCH_ITEM_STATUS_VARIANTS: Record<BatchItemStatus, NonNullable<BadgeProps['variant']>> = {
  pending_upload: 'secondary',
  uploading: 'info',
  queued: 'secondary',
  processing: 'info',
  completed: 'success',
  completed_with_warnings: 'warning',
  failed: 'danger',
  upload_failed: 'danger',
  cancelled: 'muted',
};

export function BatchStatusBadge({
  status,
  showDot = false,
  className,
}: {
  status: BatchStatus;
  showDot?: boolean;
  className?: string;
}) {
  const live = LIVE_BATCH_STATUSES.includes(status);
  return (
    <Badge variant={BATCH_STATUS_VARIANTS[status] ?? 'secondary'} className={className}>
      {showDot ? (
        <span
          aria-hidden="true"
          className={cn('size-1.5 rounded-full bg-current', live && 'animate-pulse motion-reduce:animate-none')}
        />
      ) : null}
      {BATCH_STATUS_LABELS[status] ?? status}
    </Badge>
  );
}

export function BatchItemStatusBadge({
  status,
  className,
}: {
  status: BatchItemStatus;
  className?: string;
}) {
  return (
    <Badge variant={BATCH_ITEM_STATUS_VARIANTS[status] ?? 'secondary'} className={className}>
      {BATCH_ITEM_STATUS_LABELS[status] ?? status}
    </Badge>
  );
}

function percentOf(completed: number, total: number): number {
  if (!Number.isFinite(completed) || !Number.isFinite(total) || total <= 0) return 0;
  return Math.min(100, Math.max(0, (completed / total) * 100));
}

export interface BatchSummaryProps {
  batch: BatchSummary;
  className?: string;
}

export function BatchSummaryPanel({ batch, className }: BatchSummaryProps) {
  const { counts, progress } = batch;
  const percent =
    progress.pages_total > 0
      ? percentOf(progress.pages_completed, progress.pages_total)
      : percentOf(progress.finished_items, counts.total_items);

  const cells: Array<{ label: string; value: number }> = [
    { label: 'Concluídos', value: counts.completed },
    { label: 'Com avisos', value: counts.with_warnings },
    { label: 'Processando', value: counts.processing },
    { label: 'Na fila', value: counts.queued },
    { label: 'Enviando', value: counts.uploading },
    { label: 'Aguardando envio', value: counts.pending_upload },
    { label: 'Falhas', value: counts.failed },
    { label: 'Cancelados', value: counts.cancelled },
  ];

  return (
    <Card className={className}>
      <CardContent className="space-y-4 p-5 pt-5 sm:p-5">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="space-y-1">
            <h2 className="text-sm font-semibold text-foreground">Resumo do lote</h2>
            <p className="text-xs text-muted-2" aria-live="polite">
              {formatNumber(counts.total_items)} arquivo{counts.total_items === 1 ? '' : 's'} ·{' '}
              {formatNumber(progress.finished_items)} finalizado
              {progress.finished_items === 1 ? '' : 's'}
            </p>
          </div>
          <BatchStatusBadge status={batch.status} showDot />
        </div>

        <div className="space-y-1.5">
          <div className="flex flex-wrap items-baseline justify-between gap-2 text-xs">
            <span className="text-muted-2">Páginas concluídas</span>
            <span className="tabular-nums text-foreground/80">
              {formatNumber(progress.pages_completed)} de {formatNumber(progress.pages_total)}
              {progress.pages_failed > 0 ? ` · ${formatNumber(progress.pages_failed)} com falha` : ''}
            </span>
          </div>
          <Progress
            value={percent}
            aria-label={`Progresso do lote: ${Math.round(percent)}%`}
          />
        </div>

        <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {cells.map((cell) => (
            <div
              key={cell.label}
              className="rounded-lg border border-border bg-surface-2/60 px-3 py-2"
            >
              <dt className="text-[10px] font-medium uppercase tracking-wide text-muted-2">{cell.label}</dt>
              <dd className="text-sm font-semibold tabular-nums text-foreground">{formatNumber(cell.value)}</dd>
            </div>
          ))}
        </dl>
      </CardContent>
    </Card>
  );
}
