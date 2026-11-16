import type { ConversionStatus, QualityState, Severity } from '@pdf2x/contracts';
import { Badge } from '@/components/ui/badge';
import type { BadgeProps } from '@/components/ui/badge';
import { QUALITY_LABELS, SEVERITY_LABELS, STATUS_LABELS } from '@/lib/labels';
import { cn } from '@/lib/utils';

const statusVariant: Record<ConversionStatus, NonNullable<BadgeProps['variant']>> = {
  received: 'secondary',
  validating: 'info',
  queued: 'secondary',
  inspecting: 'info',
  extracting: 'info',
  structuring: 'info',
  normalizing: 'info',
  reconciling: 'info',
  generating: 'info',
  auditing: 'default',
  completed: 'success',
  completed_with_warnings: 'warning',
  failed: 'danger',
  cancelled: 'muted',
};

export function StatusBadge({
  status,
  className,
  showDot = false,
}: {
  status: ConversionStatus;
  className?: string;
  showDot?: boolean;
}) {
  const isLive = !['completed', 'completed_with_warnings', 'failed', 'cancelled'].includes(status);
  return (
    <Badge variant={statusVariant[status] ?? 'secondary'} className={className}>
      {showDot ? (
        <span
          aria-hidden="true"
          className={cn(
            'size-1.5 rounded-full bg-current',
            isLive && 'animate-pulse motion-reduce:animate-none',
          )}
        />
      ) : null}
      {STATUS_LABELS[status] ?? status}
    </Badge>
  );
}

const qualityVariant: Record<QualityState, NonNullable<BadgeProps['variant']>> = {
  validated: 'success',
  partial: 'warning',
  failed: 'danger',
  pending: 'secondary',
};

export function QualityBadge({ quality, className }: { quality: QualityState; className?: string }) {
  return (
    <Badge variant={qualityVariant[quality] ?? 'secondary'} className={className}>
      Qualidade: {QUALITY_LABELS[quality] ?? quality}
    </Badge>
  );
}

const severityVariant: Record<Severity, NonNullable<BadgeProps['variant']>> = {
  info: 'info',
  warning: 'warning',
  error: 'danger',
  critical: 'danger',
};

export function SeverityBadge({ severity, className }: { severity: Severity; className?: string }) {
  return (
    <Badge variant={severityVariant[severity] ?? 'secondary'} className={className}>
      {SEVERITY_LABELS[severity] ?? severity}
    </Badge>
  );
}
