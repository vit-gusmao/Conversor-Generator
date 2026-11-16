import * as React from 'react';
import { AlertTriangle, CheckCircle2, Info, XCircle } from 'lucide-react';
import { cn } from '@/lib/utils';

type AlertVariant = 'info' | 'success' | 'warning' | 'error';

const styles: Record<AlertVariant, { box: string; icon: React.ReactNode }> = {
  info: {
    box: 'border-info/30 bg-info-soft/60 text-[#cfe3ff]',
    icon: <Info className="size-4 text-info" aria-hidden="true" />,
  },
  success: {
    box: 'border-success/30 bg-success-soft/60 text-[#c8f7e7]',
    icon: <CheckCircle2 className="size-4 text-success" aria-hidden="true" />,
  },
  warning: {
    box: 'border-warning/30 bg-warning-soft/60 text-[#ffedb8]',
    icon: <AlertTriangle className="size-4 text-warning" aria-hidden="true" />,
  },
  error: {
    box: 'border-danger/30 bg-danger-soft/60 text-[#ffd0d0]',
    icon: <XCircle className="size-4 text-danger" aria-hidden="true" />,
  },
};

export interface AlertProps extends React.HTMLAttributes<HTMLDivElement> {
  variant?: AlertVariant;
  title?: string;
}

export const Alert = React.forwardRef<HTMLDivElement, AlertProps>(
  ({ className, variant = 'info', title, children, ...props }, ref) => {
    const style = styles[variant];
    return (
      <div
        ref={ref}
        role="alert"
        className={cn('flex items-start gap-3 rounded-xl border px-4 py-3 text-sm', style.box, className)}
        {...props}
      >
        <span className="mt-0.5 shrink-0">{style.icon}</span>
        <div className="min-w-0 flex-1">
          {title ? <p className="font-semibold">{title}</p> : null}
          {children ? <div className={cn(title && 'mt-0.5', 'break-words opacity-90')}>{children}</div> : null}
        </div>
      </div>
    );
  },
);
Alert.displayName = 'Alert';
