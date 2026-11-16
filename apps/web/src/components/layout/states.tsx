import type { ReactNode } from 'react';
import { AlertTriangle, Inbox, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Skeleton, SkeletonText } from '@/components/ui/skeleton';
import { cn } from '@/lib/utils';

export function LoadingBlock({ label = 'Carregando…', className }: { label?: string; className?: string }) {
  return (
    <div
      className={cn('rounded-[var(--radius-card)] border border-border bg-surface/70 p-6', className)}
      role="status"
      aria-label={label}
    >
      <Skeleton className="mb-4 h-4 w-40" />
      <SkeletonText lines={4} />
      <span className="sr-only">{label}</span>
    </div>
  );
}

export function ErrorBlock({
  title = 'Não foi possível carregar',
  message,
  onRetry,
  className,
}: {
  title?: string;
  message: string;
  onRetry?: () => void;
  className?: string;
}) {
  return (
    <div
      role="alert"
      className={cn(
        'flex flex-col gap-3 rounded-[var(--radius-card)] border border-danger/30 bg-danger-soft/40 p-6',
        className,
      )}
    >
      <div className="flex items-center gap-2 text-sm font-semibold text-[#ffd0d0]">
        <AlertTriangle className="size-4 text-danger" aria-hidden="true" />
        {title}
      </div>
      <p className="text-sm text-[#ffd0d0]/80">{message}</p>
      {onRetry ? (
        <div>
          <Button variant="secondary" size="sm" onClick={onRetry}>
            <RefreshCw aria-hidden="true" />
            Tentar novamente
          </Button>
        </div>
      ) : null}
    </div>
  );
}

export function EmptyBlock({
  title,
  description,
  action,
  icon,
  className,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
  icon?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        'flex flex-col items-center justify-center gap-3 rounded-[var(--radius-card)] border border-dashed border-border bg-surface/40 px-6 py-12 text-center',
        className,
      )}
    >
      <span className="text-muted-2">{icon ?? <Inbox className="size-6" aria-hidden="true" />}</span>
      <div className="space-y-1">
        <p className="text-sm font-medium text-foreground">{title}</p>
        {description ? <p className="mx-auto max-w-md text-xs text-muted">{description}</p> : null}
      </div>
      {action}
    </div>
  );
}
