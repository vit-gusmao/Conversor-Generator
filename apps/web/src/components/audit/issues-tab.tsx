import type { AuditIssue, Severity } from '@pdf2x/contracts';
import { ArrowUpRight } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { EmptyBlock, ErrorBlock, LoadingBlock } from '@/components/layout/states';
import { SeverityBadge } from '@/components/status-badge';
import { useAuditIssues } from '@/hooks/use-audit';
import { explainError } from '@/lib/errors';
import { formatDateTime, formatNumber } from '@/lib/format';
import { SEVERITY_LABELS } from '@/lib/labels';

const ORDER: Severity[] = ['critical', 'error', 'warning', 'info'];

export function IssuesTab({
  conversionId,
  onOpenPage,
}: {
  conversionId: string;
  onOpenPage: (page: number) => void;
}) {
  const issues = useAuditIssues(conversionId);

  if (issues.isPending) return <LoadingBlock label="Carregando inconsistências" />;
  if (issues.isError) return <ErrorBlock message={explainError(issues.error)} onRetry={() => void issues.refetch()} />;

  const items = issues.data?.items ?? [];
  if (items.length === 0) {
    return (
      <EmptyBlock
        title="Nenhuma inconsistência registrada"
        description="A execução não publicou erros, avisos ou regiões incertas."
      />
    );
  }

  const grouped = new Map<Severity, AuditIssue[]>();
  for (const issue of items) {
    const list = grouped.get(issue.severity) ?? [];
    list.push(issue);
    grouped.set(issue.severity, list);
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-sm text-muted">
          {formatNumber(issues.data?.total ?? items.length)} inconsistências agrupadas por gravidade.
        </p>
      </div>

      {ORDER.filter((severity) => (grouped.get(severity)?.length ?? 0) > 0).map((severity) => {
        const group = grouped.get(severity) ?? [];
        return (
          <Card key={severity}>
            <CardHeader className="pb-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <CardTitle className="flex items-center gap-2">
                  <SeverityBadge severity={severity} />
                  {SEVERITY_LABELS[severity]}
                </CardTitle>
                <Badge variant="secondary">{formatNumber(group.length)}</Badge>
              </div>
            </CardHeader>
            <CardContent className="space-y-2 pt-0">
              {group.map((issue) => (
                <div
                  key={issue.issue_id}
                  className="flex flex-wrap items-start gap-3 rounded-xl border border-border bg-surface-2/30 p-3"
                >
                  <span className="font-mono text-[10px] text-muted-2">{issue.code}</span>
                  <div className="min-w-0 flex-1">
                    <p className="text-sm text-foreground/90">{issue.message}</p>
                    <p className="mt-0.5 text-[11px] text-muted-2">
                      {formatDateTime(issue.created_at)}
                      {issue.region_id ? ` · região ${issue.region_id}` : ''}
                    </p>
                  </div>
                  {issue.page_number !== null ? (
                    <button
                      type="button"
                      onClick={() => onOpenPage(issue.page_number as number)}
                      className="inline-flex items-center gap-1 text-xs font-medium text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                    >
                      Página {issue.page_number}
                      <ArrowUpRight className="size-3" aria-hidden="true" />
                    </button>
                  ) : (
                    <span className="text-[11px] text-muted-2">sem página</span>
                  )}
                </div>
              ))}
            </CardContent>
          </Card>
        );
      })}
    </div>
  );
}
