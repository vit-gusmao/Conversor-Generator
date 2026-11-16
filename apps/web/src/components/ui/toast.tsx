import type * as React from 'react';
import { AlertTriangle, CheckCircle2, Info, X, XCircle } from 'lucide-react';
import { useToastStore } from '@/stores/ui';
import type { ToastVariant } from '@/stores/ui';
import { cn } from '@/lib/utils';

const variantStyles: Record<ToastVariant, { box: string; icon: React.ReactNode }> = {
  success: {
    box: 'border-success/40',
    icon: <CheckCircle2 className="size-4 text-success" aria-hidden="true" />,
  },
  error: { box: 'border-danger/40', icon: <XCircle className="size-4 text-danger" aria-hidden="true" /> },
  warning: {
    box: 'border-warning/40',
    icon: <AlertTriangle className="size-4 text-warning" aria-hidden="true" />,
  },
  info: { box: 'border-info/40', icon: <Info className="size-4 text-info" aria-hidden="true" /> },
};

export function ToastViewport() {
  const toasts = useToastStore((state) => state.toasts);
  const dismiss = useToastStore((state) => state.dismiss);

  return (
    <div
      aria-live="polite"
      aria-atomic="false"
      className="pointer-events-none fixed bottom-4 right-4 z-[100] flex w-[calc(100%-2rem)] max-w-sm flex-col gap-2"
    >
      {toasts.map((toast) => {
        const style = variantStyles[toast.variant];
        return (
          <div
            key={toast.id}
            role="status"
            className={cn(
              'pointer-events-auto flex items-start gap-3 rounded-xl border bg-surface-3/95 p-3.5 shadow-2xl backdrop-blur animate-slide-up',
              style.box,
            )}
          >
            <span className="mt-0.5 shrink-0">{style.icon}</span>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium text-foreground">{toast.title}</p>
              {toast.description ? (
                <p className="mt-0.5 break-words text-xs text-muted">{toast.description}</p>
              ) : null}
            </div>
            <button
              type="button"
              onClick={() => dismiss(toast.id)}
              aria-label="Fechar notificação"
              className="rounded-md p-1 text-muted-2 transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              <X className="size-3.5" />
            </button>
          </div>
        );
      })}
    </div>
  );
}
