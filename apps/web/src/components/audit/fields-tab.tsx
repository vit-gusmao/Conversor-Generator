import { useMemo } from 'react';
import type { AuditSummary, BlockAudit, PageAudit } from '@pdf2x/contracts';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { EmptyBlock, ErrorBlock, LoadingBlock } from '@/components/layout/states';
import { useAggregatedPageAudits } from '@/hooks/use-audit';
import { usePreview, useResult } from '@/hooks/use-conversions';
import { explainError } from '@/lib/errors';
import { cellAddress, formatNumber } from '@/lib/format';
import { ORIGIN_LABELS } from '@/lib/labels';
import { cn } from '@/lib/utils';

interface FieldRow {
  key: string;
  label: string;
  value: string;
  page: number;
  block: BlockAudit;
}

function sortedBlocks(page: PageAudit): BlockAudit[] {
  return [...page.blocks].sort((a, b) => a.bbox[1] - b.bbox[1] || a.bbox[0] - b.bbox[0]);
}

function extractFieldPairs(pages: PageAudit[]): FieldRow[] {
  const rows: FieldRow[] = [];
  for (const page of pages) {
    const blocks = sortedBlocks(page);
    for (let index = 0; index < blocks.length; index += 1) {
      const block = blocks[index];
      if (!block || block.destination?.kind !== 'field') continue;

      const inline = /^([^:]{1,80}):\s*(.+)$/.exec(block.text.trim());
      if (inline && inline[1] && inline[2]) {
        rows.push({
          key: `${page.page_number}-${block.element_id}`,
          label: inline[1].trim(),
          value: inline[2].trim(),
          page: page.page_number,
          block,
        });
        continue;
      }

      const previous = blocks[index - 1];
      rows.push({
        key: `${page.page_number}-${block.element_id}`,
        label: previous && previous.text.length <= 80 ? previous.text.trim() : '—',
        value: block.text.trim(),
        page: page.page_number,
        block,
      });
    }
  }
  return rows;
}

export function FieldsTab({ conversionId, summary }: { conversionId: string; summary: AuditSummary }) {
  const pages = useAggregatedPageAudits(conversionId, summary.metrics.pages_total);
  const result = useResult(conversionId);
  const recordsSheet =
    result.data?.sheets.find((sheet) => sheet.kind === 'records') ??
    result.data?.sheets.find((sheet) => sheet.kind === 'table') ??
    null;
  const preview = usePreview(
    conversionId,
    { sheet: recordsSheet?.name ?? null, offset: 0, limit: 50 },
    Boolean(recordsSheet),
  );

  const fields = useMemo(() => extractFieldPairs(pages.data), [pages.data]);

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="pb-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle>Campos extraídos</CardTitle>
            <span className="text-xs text-muted-2">
              {formatNumber(fields.length)} campos · {formatNumber(summary.metrics.fields_detected)} detectados
            </span>
          </div>
        </CardHeader>
        <CardContent className="pt-0">
          {pages.isLoading ? (
            <LoadingBlock label="Carregando campos extraídos" />
          ) : pages.error ? (
            <ErrorBlock message={explainError(pages.error)} onRetry={pages.refetch} />
          ) : fields.length === 0 ? (
            <EmptyBlock
              title="Nenhum campo associado"
              description="Os blocos desta conversão não foram classificados como campo. Veja Dados Extraídos para todos os blocos."
            />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Rótulo</TableHead>
                  <TableHead>Valor</TableHead>
                  <TableHead>Página</TableHead>
                  <TableHead>Evidência</TableHead>
                  <TableHead>Destino</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {fields.map((row) => (
                  <TableRow key={row.key}>
                    <TableCell className="text-xs font-medium text-foreground">{row.label}</TableCell>
                    <TableCell className="max-w-80 text-sm">{row.value}</TableCell>
                    <TableCell className="text-xs text-muted">{row.page}</TableCell>
                    <TableCell className="text-[11px] text-muted-2">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <Badge variant="outline">{ORIGIN_LABELS[row.block.origin] ?? row.block.origin}</Badge>
                        <span className="font-mono">
                          [{row.block.bbox.map((value) => Math.round(value)).join(', ')}]
                        </span>
                        {row.block.confidence !== null ? (
                          <span>conf. {(row.block.confidence * 100).toFixed(0)}%</span>
                        ) : null}
                      </div>
                      <p className="mt-0.5 font-mono text-[10px]">{row.block.element_id}</p>
                    </TableCell>
                    <TableCell className="text-xs">
                      {row.block.destination?.sheet ? (
                        <span className="font-mono text-[10px] text-foreground/80">
                          {row.block.destination.sheet}
                          {row.block.destination.cell ? `!${row.block.destination.cell}` : ''}
                        </span>
                      ) : (
                        <span className="text-muted-2">—</span>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
          {pages.truncated ? (
            <p className="mt-2 text-[11px] text-warning">
              Exibindo as primeiras 25 páginas de {formatNumber(summary.metrics.pages_total)}. Use a aba Páginas
              para inspecionar as demais.
            </p>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle>Registros da planilha</CardTitle>
            {recordsSheet ? (
              <span className="text-xs text-muted-2">
                aba <span className="font-mono">{recordsSheet.name}</span>
              </span>
            ) : null}
          </div>
        </CardHeader>
        <CardContent className="pt-0">
          {result.isPending ? (
            <LoadingBlock label="Carregando registros" />
          ) : result.isError ? (
            <p className="text-xs text-muted-2">Metadados do resultado indisponíveis para esta conversão.</p>
          ) : !recordsSheet ? (
            <EmptyBlock
              title="Nenhuma aba de registros"
              description="Esta conversão não gerou uma aba estruturada em registros."
            />
          ) : preview.isPending ? (
            <LoadingBlock label="Carregando prévia de registros" />
          ) : preview.isError ? (
            <ErrorBlock message={explainError(preview.error)} onRetry={() => void preview.refetch()} />
          ) : !preview.data ? (
            <EmptyBlock title="Sem registros" />
          ) : (
            <div className="overflow-hidden rounded-xl border border-border">
              <Table>
                <TableHeader className="bg-surface-2">
                  <TableRow>
                    <TableHead className="w-12 text-right">#</TableHead>
                    {preview.data.headers.map((header, index) => (
                      <TableHead key={`${header}-${index}`}>{header || `Coluna ${index + 1}`}</TableHead>
                    ))}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {preview.data.rows.map((row, rowIndex) => (
                    <TableRow key={rowIndex}>
                      <TableCell className="text-right font-mono text-[10px] text-muted-2">
                        {rowIndex + 2}
                      </TableCell>
                      {row.map((cell, columnIndex) => (
                        <TableCell
                          key={`${cellAddress(rowIndex, columnIndex)}-${columnIndex}`}
                          className={cn(
                            'max-w-60 text-xs',
                            (cell.type === 'integer' || cell.type === 'decimal' || cell.type === 'money') &&
                              'text-right font-mono tabular-nums',
                          )}
                        >
                          {cell.display || '—'}
                        </TableCell>
                      ))}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
