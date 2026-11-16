import { useMemo } from 'react';
import type { AuditSummary, TableAudit } from '@pdf2x/contracts';
import { ArrowRight, Table2 } from 'lucide-react';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { EmptyBlock, ErrorBlock, LoadingBlock } from '@/components/layout/states';
import { useAggregatedPageAudits } from '@/hooks/use-audit';
import { explainError } from '@/lib/errors';
import { formatNumber } from '@/lib/format';
import { STRATEGY_LABELS } from '@/lib/labels';

interface TableWithPage extends TableAudit {
  fallbackPage: number;
}

export function TablesTab({
  conversionId,
  summary,
  onOpenPage,
}: {
  conversionId: string;
  summary: AuditSummary;
  onOpenPage: (page: number) => void;
}) {
  const pages = useAggregatedPageAudits(conversionId, summary.metrics.pages_total);
  const tables = useMemo<TableWithPage[]>(
    () =>
      pages.data.flatMap((page) =>
        page.tables_detail.map((table) => ({ ...table, fallbackPage: page.page_number })),
      ),
    [pages.data],
  );

  if (pages.isLoading) return <LoadingBlock label="Carregando tabelas" />;
  if (pages.error) return <ErrorBlock message={explainError(pages.error)} onRetry={pages.refetch} />;
  if (tables.length === 0) {
    return (
      <EmptyBlock
        icon={<Table2 className="size-6" aria-hidden="true" />}
        title="Nenhuma tabela detectada"
        description="Esta conversão gerou apenas texto ou campos isolados. Veja a aba Dados Extraídos."
      />
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-muted">
          {formatNumber(tables.length)} tabelas · {formatNumber(summary.metrics.cells_generated)} células geradas
        </p>
        {pages.truncated ? (
          <p className="text-[11px] text-warning">
            Exibindo tabelas das primeiras {pages.pageCount} páginas.
          </p>
        ) : null}
      </div>

      {tables.map((table) => {
        const page = table.page_number ?? table.fallbackPage;
        return (
          <Card key={`${table.table_id}-${page}`}>
            <CardContent className="space-y-3 p-5 pt-5 sm:p-5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-xs text-muted-2">{table.table_id}</span>
                <Badge variant="outline">{STRATEGY_LABELS[table.strategy] ?? table.strategy}</Badge>
                <button
                  type="button"
                  onClick={() => onOpenPage(page)}
                  className="inline-flex items-center gap-1 text-xs text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                >
                  página {page}
                  <ArrowRight className="size-3" aria-hidden="true" />
                </button>
                <span className="ml-auto text-xs text-muted-2">
                  {formatNumber(table.rows_count)} × {formatNumber(table.columns_count)} · confiança{' '}
                  {(table.confidence * 100).toFixed(0)}%
                </span>
              </div>

              <div className="flex flex-wrap items-center gap-2 text-xs">
                {table.sheet ? (
                  <Badge variant="secondary">
                    {table.sheet}
                    {table.cell_range ? `!${table.cell_range}` : ''}
                  </Badge>
                ) : (
                  <Badge variant="muted">sem aba de destino</Badge>
                )}
                {table.continued_from_table_id ? (
                  <span className="text-muted-2">continua de {table.continued_from_table_id}</span>
                ) : null}
                {table.continued_on_page !== null ? (
                  <button
                    type="button"
                    onClick={() => onOpenPage(table.continued_on_page as number)}
                    className="text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                  >
                    continua na página {table.continued_on_page}
                  </button>
                ) : null}
                {table.merged_cells.length > 0 ? (
                  <Badge variant="warning">{table.merged_cells.length} células mescladas</Badge>
                ) : null}
              </div>

              {table.headers.length > 0 ? (
                <div>
                  <p className="mb-1 text-[11px] uppercase tracking-wide text-muted-2">
                    Cabeçalhos {table.headers.some((header) => header) ? 'detectados' : 'inferidos'}
                  </p>
                  <div className="flex flex-wrap gap-1.5">
                    {table.headers.map((header, index) => (
                      <Badge key={`${header}-${index}`} variant="secondary">
                        {header || `Coluna ${index + 1}`}
                      </Badge>
                    ))}
                  </div>
                </div>
              ) : null}

              {table.alternatives.length > 0 ? (
                <div>
                  <p className="mb-1 text-[11px] uppercase tracking-wide text-muted-2">
                    Estratégias comparadas
                  </p>
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Estratégia</TableHead>
                        <TableHead className="text-right">Confiança</TableHead>
                        <TableHead>Resultado</TableHead>
                        <TableHead>Motivo</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {table.alternatives.map((alternative, index) => (
                        <TableRow key={`${alternative.strategy}-${index}`}>
                          <TableCell className="text-xs">{alternative.strategy}</TableCell>
                          <TableCell className="text-right font-mono text-xs tabular-nums">
                            {(alternative.confidence * 100).toFixed(0)}%
                          </TableCell>
                          <TableCell>
                            <Badge variant={alternative.accepted ? 'success' : 'muted'}>
                              {alternative.accepted ? 'aceita' : 'rejeitada'}
                            </Badge>
                          </TableCell>
                          <TableCell className="text-xs text-muted">{alternative.reason}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              ) : (
                <p className="text-[11px] text-muted-2">Nenhuma estratégia alternativa registrada.</p>
              )}

              {table.merged_cells.length > 0 ? (
                <details className="rounded-lg border border-border p-2.5 text-xs">
                  <summary className="cursor-pointer text-muted">Células mescladas</summary>
                  <ul className="mt-2 flex flex-wrap gap-1.5">
                    {table.merged_cells.map((merged, index) => (
                      <li key={`${merged.row}-${merged.col}-${index}`}>
                        <Badge variant="outline">
                          r{merged.row}c{merged.col} · {merged.rowspan}×{merged.colspan}
                        </Badge>
                      </li>
                    ))}
                  </ul>
                </details>
              ) : null}
            </CardContent>
          </Card>
        );
      })}
    </div>
  );
}
