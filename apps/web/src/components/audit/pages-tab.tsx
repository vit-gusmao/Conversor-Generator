import { FileSearch, ScanLine, Table2 } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { EmptyBlock, ErrorBlock, LoadingBlock } from '@/components/layout/states';
import { SeverityBadge } from '@/components/status-badge';
import { PageNav } from '@/components/audit/page-nav';
import { usePageAudit } from '@/hooks/use-audit';
import { explainError } from '@/lib/errors';
import { formatNumber, truncate } from '@/lib/format';
import {
  CLASSIFICATION_LABELS,
  DESTINATION_LABELS,
  ORIGIN_LABELS,
  STRATEGY_LABELS,
} from '@/lib/labels';

function bboxLabel(bbox: [number, number, number, number]): string {
  return bbox.map((value) => Math.round(value)).join(', ');
}

export function PagesTab({
  conversionId,
  page,
  totalPages,
  onPageChange,
}: {
  conversionId: string;
  page: number;
  totalPages: number;
  onPageChange: (page: number) => void;
}) {
  const audit = usePageAudit(conversionId, page);
  const data = audit.data;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <PageNav page={page} total={totalPages} onChange={onPageChange} busy={audit.isFetching} />
        {data ? (
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <Badge variant="outline">{CLASSIFICATION_LABELS[data.classification] ?? data.classification}</Badge>
            <Badge variant={data.status === 'ok' ? 'success' : data.status === 'failed' ? 'danger' : 'warning'}>
              {data.status}
            </Badge>
            <span className="text-muted-2">fonte: {data.text_source}</span>
          </div>
        ) : null}
      </div>

      {audit.isPending ? (
        <LoadingBlock label={`Carregando auditoria da página ${page}`} />
      ) : audit.isError ? (
        <ErrorBlock
          title="Não foi possível carregar a página"
          message={explainError(audit.error)}
          onRetry={() => void audit.refetch()}
        />
      ) : !data ? (
        <EmptyBlock title="Página sem auditoria" description="A API não retornou dados para esta página." />
      ) : (
        <>
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2">
                <FileSearch className="size-4 text-accent" aria-hidden="true" />
                Página {data.page_number} de {formatNumber(totalPages)}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 pt-0">
              <dl className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
                <div className="rounded-lg border border-border bg-surface-2/40 p-2.5">
                  <dt className="text-muted-2">Dimensões</dt>
                  <dd className="font-mono text-foreground">
                    {Math.round(data.width)} × {Math.round(data.height)} pt
                  </dd>
                </div>
                <div className="rounded-lg border border-border bg-surface-2/40 p-2.5">
                  <dt className="text-muted-2">Rotação</dt>
                  <dd className="font-mono text-foreground">{data.rotation}°</dd>
                </div>
                <div className="rounded-lg border border-border bg-surface-2/40 p-2.5">
                  <dt className="text-muted-2">Caracteres nativos</dt>
                  <dd className="font-mono tabular-nums text-foreground">{formatNumber(data.native_chars)}</dd>
                </div>
                <div className="rounded-lg border border-border bg-surface-2/40 p-2.5">
                  <dt className="text-muted-2">Caracteres OCR</dt>
                  <dd className="font-mono tabular-nums text-foreground">{formatNumber(data.ocr_chars)}</dd>
                </div>
              </dl>

              <section aria-labelledby="page-regions">
                <h4 id="page-regions" className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-2">
                  Regiões detectadas ({data.regions.length})
                </h4>
                {data.regions.length === 0 ? (
                  <p className="text-xs text-muted-2">Nenhuma região registrada nesta página.</p>
                ) : (
                  <ul className="grid gap-2 sm:grid-cols-2">
                    {data.regions.map((region) => (
                      <li key={region.region_id} className="rounded-lg border border-border p-2.5 text-xs">
                        <div className="flex flex-wrap items-center gap-2">
                          <Badge variant="outline">{region.kind}</Badge>
                          <span className="font-mono text-[10px] text-muted-2">{region.region_id}</span>
                          <span className="ml-auto text-muted-2">
                            confiança {(region.confidence * 100).toFixed(0)}%
                          </span>
                        </div>
                        <p className="mt-1 text-muted-2">
                          {region.engine} · {region.resolution} · bbox [{bboxLabel(region.bbox)}]
                        </p>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2">
                <ScanLine className="size-4 text-accent" aria-hidden="true" />
                Blocos de texto ({data.blocks.length})
              </CardTitle>
            </CardHeader>
            <CardContent className="pt-0">
              {data.blocks.length === 0 ? (
                <EmptyBlock title="Nenhum bloco de texto" description="A página não produziu blocos textuais." />
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Texto</TableHead>
                      <TableHead>bbox</TableHead>
                      <TableHead>Fonte</TableHead>
                      <TableHead>Origem</TableHead>
                      <TableHead className="text-right">Confiança</TableHead>
                      <TableHead>Destino</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data.blocks.map((block) => (
                      <TableRow key={block.element_id}>
                        <TableCell className="max-w-80">
                          <span title={block.text}>{truncate(block.text, 90)}</span>
                          <p className="mt-0.5 font-mono text-[10px] text-muted-2">{block.element_id}</p>
                        </TableCell>
                        <TableCell className="whitespace-nowrap font-mono text-[10px] text-muted">
                          [{bboxLabel(block.bbox)}]
                        </TableCell>
                        <TableCell className="whitespace-nowrap text-xs text-muted">
                          {block.font ?? '—'}
                          {block.size !== null ? ` · ${block.size}pt` : ''}
                        </TableCell>
                        <TableCell>
                          <Badge variant={block.origin === 'ocr' ? 'warning' : 'secondary'}>
                            {ORIGIN_LABELS[block.origin] ?? block.origin}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-right font-mono text-[11px] tabular-nums">
                          {block.confidence === null ? '—' : `${(block.confidence * 100).toFixed(0)}%`}
                        </TableCell>
                        <TableCell className="text-xs">
                          {block.destination ? (
                            <span className="inline-flex flex-wrap items-center gap-1.5">
                              <Badge variant="outline">{DESTINATION_LABELS[block.destination.kind] ?? block.destination.kind}</Badge>
                              {block.destination.sheet ? (
                                <span className="font-mono text-[10px] text-muted-2">
                                  {block.destination.sheet}
                                  {block.destination.cell ? `!${block.destination.cell}` : ''}
                                </span>
                              ) : null}
                            </span>
                          ) : (
                            <span className="text-muted-2">sem destino</span>
                          )}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>

          <div className="grid gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="flex items-center gap-2">
                  <Table2 className="size-4 text-accent" aria-hidden="true" />
                  Tabelas na página ({data.tables_detail.length})
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 pt-0">
                {data.tables_detail.length === 0 ? (
                  <p className="text-xs text-muted-2">Nenhuma tabela detectada nesta página.</p>
                ) : (
                  data.tables_detail.map((table) => (
                    <div key={table.table_id} className="rounded-lg border border-border p-2.5 text-xs">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-[10px] text-muted-2">{table.table_id}</span>
                        <Badge variant="outline">{STRATEGY_LABELS[table.strategy] ?? table.strategy}</Badge>
                        <span className="ml-auto text-muted-2">
                          {table.rows_count} × {table.columns_count}
                        </span>
                      </div>
                      <p className="mt-1 text-muted-2">
                        {table.sheet ?? 'sem aba'}
                        {table.cell_range ? ` · ${table.cell_range}` : ''} · confiança{' '}
                        {(table.confidence * 100).toFixed(0)}%
                      </p>
                    </div>
                  ))
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardTitle>Inconsistências da página ({data.issues.length})</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 pt-0">
                {data.issues.length === 0 ? (
                  <p className="text-xs text-muted-2">Nenhuma inconsistência nesta página.</p>
                ) : (
                  data.issues.map((issue) => (
                    <div key={issue.issue_id} className="rounded-lg border border-border p-2.5 text-xs">
                      <div className="flex flex-wrap items-center gap-2">
                        <SeverityBadge severity={issue.severity} />
                        <span className="font-mono text-[10px] text-muted-2">{issue.code}</span>
                      </div>
                      <p className="mt-1 text-foreground/85">{issue.message}</p>
                    </div>
                  ))
                )}
              </CardContent>
            </Card>
          </div>
        </>
      )}
    </div>
  );
}
