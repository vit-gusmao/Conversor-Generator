import * as React from 'react';
import * as ProgressPrimitive from '@radix-ui/react-progress';
import { cn } from '@/lib/utils';

export interface ProgressProps
  extends React.ComponentPropsWithoutRef<typeof ProgressPrimitive.Root> {
  value?: number;
  indeterminate?: boolean;
}

export const Progress = React.forwardRef<
  React.ComponentRef<typeof ProgressPrimitive.Root>,
  ProgressProps
>(({ className, value = 0, indeterminate = false, ...props }, ref) => {
  const bounded = Math.min(100, Math.max(0, Number.isFinite(value) ? value : 0));
  return (
    <ProgressPrimitive.Root
      ref={ref}
      value={indeterminate ? undefined : bounded}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={indeterminate ? undefined : Math.round(bounded)}
      className={cn('relative h-2 w-full overflow-hidden rounded-full bg-surface-3', className)}
      {...props}
    >
      <ProgressPrimitive.Indicator
        className={cn(
          'h-full rounded-full bg-gradient-to-r from-accent-strong to-accent transition-[width] duration-500 ease-out',
          indeterminate && 'indeterminate-bar w-1/3',
        )}
        style={indeterminate ? undefined : { width: `${bounded}%` }}
      />
    </ProgressPrimitive.Root>
  );
});
Progress.displayName = 'Progress';
