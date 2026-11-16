import type { AuditSummary } from '@pdf2x/contracts';
import { Clock } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { EmptyBlock } from '@/components/layout/states';
import { formatDateTime, formatDuration, formatNumber } from '@/lib/format';
import { STAGE_LABELS } from '@/lib/labels';
import { cn } from '@/lib/utils';

export function PerformanceTab({ summary }: { summary: AuditSummary }) {
  const timings = summary.timings;
  const total = timings.reduce((sum, timing) => sum + timing.duration_ms, 0);
  const max = timings.reduce((value, timing) => Math.max(value, timing.duration_ms), 0);
  const pages = timings.reduce((sum, timing) => sum + timing.pages, 0);

  if (timings.length === 0) {
    return (
      <EmptyBlock
        icon={<Clock className="size-6" aria-hidden="true" />}
        title="Nenhuma medição de desempenho"
        description="A execução não publicou tempos por etapa."
      />
    );
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle>Desempenho por etapa</CardTitle>
          <span className="text-xs text-muted-2">
            {formatDuration(total)} no total · {formatNumber(pages)} páginas processadas
          </span>
        </div>
      </CardHeader>
      <CardContent className="space-y-4 pt-0">
        {[...timings]
          .sort((a, b) => b.duration_ms - a.duration_ms)
          .map((timing) => {
            const width = max > 0 ? Math.max(2, (timing.duration_ms / max) * 100) : 0;
            const share = total > 0 ? (timing.duration_ms / total) * 100 : 0;
            return (
              <div key={`${timing.stage}-${timing.started_at}`} className="space-y-1.5">
                <div className="flex flex-wrap items-baseline justify-between gap-2 text-xs">
                  <span className="font-medium text-foreground">
                    {STAGE_LABELS[timing.stage] ?? timing.stage}
                  </span>
                  <span className="font-mono tabular-nums text-muted">
                    {formatDuration(timing.duration_ms)} · {share.toFixed(1)}%
                  </span>
                </div>
                <div className="h-2 w-full overflow-hidden rounded-full bg-surface-3">
                  <div
                    className={cn('h-full rounded-full bg-gradient-to-r from-accent-strong to-accent')}
                    style={{ width: `${width}%` }}
                  />
                </div>
                <div className="flex flex-wrap justify-between gap-2 text-[10px] text-muted-2">
                  <span>
                    {formatDateTime(timing.started_at)} → {formatDateTime(timing.finished_at)}
                  </span>
                  <span>{formatNumber(timing.pages)} páginas</span>
                </div>
              </div>
            );
          })}
      </CardContent>
    </Card>
  );
}
