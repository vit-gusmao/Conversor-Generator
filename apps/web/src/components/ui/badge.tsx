import * as React from 'react';
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '@/lib/utils';

const badgeVariants = cva(
  'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium leading-5 whitespace-nowrap',
  {
    variants: {
      variant: {
        default: 'border-accent/40 bg-accent-soft text-accent',
        secondary: 'border-border bg-surface-2 text-muted',
        outline: 'border-border-strong bg-transparent text-muted',
        success: 'border-success/30 bg-success-soft text-[#8ef0cd]',
        warning: 'border-warning/30 bg-warning-soft text-[#ffcf9e]',
        danger: 'border-danger/30 bg-danger-soft text-[#ffb4b4]',
        info: 'border-info/30 bg-info-soft text-[#a9cdfd]',
        muted: 'border-border bg-surface-2 text-muted-2',
      },
    },
    defaultVariants: { variant: 'default' },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { badgeVariants };
