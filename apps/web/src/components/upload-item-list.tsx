/**
 * Queue list used on the home screen.
 *
 * Rows are capped at `initialLimit` (100) and the user can reveal more, so a
 * batch with thousands of files keeps the DOM small. Every action is a real
 * `<button>` with an `aria-label` naming the file.
 */

import { useEffect, useState } from 'react';
import { FileImage, FileText, RotateCcw, StopCircle, X } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import type { BadgeProps } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { formatBytes } from '@/lib/format';
import { cn } from '@/lib/utils';
import type { UploadItem, UploadItemState } from '@/stores/upload-queue';

export const UPLOAD_ITEM_STATE_LABELS: Record<UploadItemState, string> = {
  queued: 'Na fila',
  uploading: 'Enviando',
  uploaded: 'Enviado',
  failed: 'Falhou',
  cancelled: 'Cancelado',
  needs_reselection: 'Selecione novamente',
};

const STATE_VARIANTS: Record<UploadItemState, NonNullable<BadgeProps['variant']>> = {
  queued: 'secondary',
  uploading: 'info',
  uploaded: 'success',
  failed: 'danger',
  cancelled: 'muted',
  needs_reselection: 'warning',
};

const REMOVABLE_STATES: readonly UploadItemState[] = [
  'queued',
  'failed',
  'cancelled',
  'needs_reselection',
];

const RETRYABLE_STATES: readonly UploadItemState[] = ['failed', 'cancelled'];
const CANCELLABLE_STATES: readonly UploadItemState[] = ['queued', 'uploading'];

export interface UploadItemListProps {
  items: readonly UploadItem[];
  onRemove?: (clientItemId: string) => void;
  onRetry?: (clientItemId: string) => void;
  onCancel?: (clientItemId: string) => void;
  /** Rows rendered before "mostrar mais". */
  initialLimit?: number;
  emptyMessage?: string;
  className?: string;
}

export function UploadItemList({
  items,
  onRemove,
  onRetry,
  onCancel,
  initialLimit = 100,
  emptyMessage = 'Nenhum arquivo na fila.',
  className,
}: UploadItemListProps) {
  const [limit, setLimit] = useState(initialLimit);

  // Removing rows should never leave the list "scrolled" past the data.
  useEffect(() => {
    if (limit > Math.max(initialLimit, items.length)) {
      setLimit(Math.max(initialLimit, items.length));
    }
  }, [items.length, initialLimit, limit]);

  if (items.length === 0) {
    return (
      <p className={cn('rounded-xl border border-dashed border-border bg-surface/40 px-4 py-6 text-center text-sm text-muted', className)}>
        {emptyMessage}
      </p>
    );
  }

  const visible = items.slice(0, limit);
  const remaining = items.length - visible.length;

  return (
    <div className={cn('space-y-3', className)}>
      <ul className="space-y-2" aria-label="Arquivos na fila">
        {visible.map((item) => {
          const Icon = item.kind === 'image' ? FileImage : FileText;
          const canRemove = REMOVABLE_STATES.includes(item.state) && Boolean(onRemove);
          const canRetry = RETRYABLE_STATES.includes(item.state) && Boolean(onRetry);
          const canCancel = CANCELLABLE_STATES.includes(item.state) && Boolean(onCancel);
          const showReselectionHint =
            item.state === 'needs_reselection' && !item.error;

          return (
            <li
              key={item.clientItemId}
              className="flex flex-wrap items-center gap-3 rounded-xl border border-border bg-surface/60 px-4 py-3"
            >
              <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-accent/10 text-accent">
                <Icon className="size-4" aria-hidden="true" />
              </span>

              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium text-foreground">{item.filename}</p>
                <p className="text-xs text-muted-2">
                  {formatBytes(item.size)} · {item.kind === 'image' ? 'imagem' : 'PDF'}
                  {item.state === 'uploading' && item.uploadedBytes !== undefined ? (
                    <span className="tabular-nums">
                      {' '}
                      · {formatBytes(item.uploadedBytes)} de {formatBytes(item.size)}
                    </span>
                  ) : null}
                </p>
              </div>

              <Badge variant={STATE_VARIANTS[item.state]}>{UPLOAD_ITEM_STATE_LABELS[item.state]}</Badge>

              <div className="flex items-center gap-1">
                {canRetry ? (
                  <button
                    type="button"
                    onClick={() => onRetry?.(item.clientItemId)}
                    aria-label={`Tentar novamente ${item.filename}`}
                    title="Tentar novamente"
                    className="rounded-lg p-2 text-muted-2 transition-colors hover:bg-surface-3 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                  >
                    <RotateCcw className="size-4" aria-hidden="true" />
                  </button>
                ) : null}
                {canCancel ? (
                  <button
                    type="button"
                    onClick={() => onCancel?.(item.clientItemId)}
                    aria-label={`Cancelar ${item.filename}`}
                    title="Cancelar envio"
                    className="rounded-lg p-2 text-muted-2 transition-colors hover:bg-surface-3 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                  >
                    <StopCircle className="size-4" aria-hidden="true" />
                  </button>
                ) : null}
                {canRemove ? (
                  <button
                    type="button"
                    onClick={() => onRemove?.(item.clientItemId)}
                    aria-label={`Remover ${item.filename}`}
                    title="Remover da fila"
                    className="rounded-lg p-2 text-muted-2 transition-colors hover:bg-surface-3 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                  >
                    <X className="size-4" aria-hidden="true" />
                  </button>
                ) : null}
              </div>

              {item.error || showReselectionHint ? (
                <p className="basis-full text-xs text-danger" aria-live="polite">
                  {item.error ??
                    'O arquivo original não está mais disponível após recarregar a página. Selecione-o novamente.'}
                </p>
              ) : null}
            </li>
          );
        })}
      </ul>

      {remaining > 0 ? (
        <div className="flex justify-center">
          <Button variant="secondary" size="sm" onClick={() => setLimit((current) => current + initialLimit)}>
            Mostrar mais ({remaining} restante{remaining === 1 ? '' : 's'})
          </Button>
        </div>
      ) : null}
    </div>
  );
}
