import { useEffect, useState } from 'react';
import type { SheetSummary } from '@pdf2x/contracts';
import { ChevronLeft, ChevronRight, Table2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { EmptyBlock, ErrorBlock, LoadingBlock } from '@/components/layout/states';
import { usePreview } from '@/hooks/use-conversions';
import { explainError } from '@/lib/errors';
import { cellAddress, formatNumber } from '@/lib/format';
import { SHEET_KIND_LABELS } from '@/lib/labels';
import { cn } from '@/lib/utils';

export interface SheetPreviewProps {
  conversionId: string;
  sheet: string | null;
  sheets?: SheetSummary[];
  pageSize?: number;
  onSheetChange?: (sheet: string) => void;
  onCellClick?: (cell: { sheet: string; address: string; value: string }) => void;
  selectedCell?: string | null;
  className?: string;
}

export function SheetPreview({
  conversionId,
  sheet,
  sheets = [],
  pageSize = 100,
  onSheetChange,
  onCellClick,
  selectedCell,
  className,
}: SheetPreviewProps) {
  const [offset, setOffset] = useState(0);
  const activeSheet = sheet ?? sheets[0]?.name ?? null;

  useEffect(() => {
    setOffset(0);
  }, [activeSheet]);

  const preview = usePreview(
    conversionId,
    { sheet: activeSheet, offset, limit: pageSize },
    Boolean(activeSheet),
  );

  const sheetMeta = sheets.find((item) => item.name === activeSheet) ?? null;
  const totalRows = preview.data?.total_rows ?? 0;
  const from = totalRows === 0 ? 0 : offset + 1;
  const to = Math.min(offset + pageSize, totalRows);

  return (
    <div className={cn('space-y-3', className)}>
      {(sheets.length > 1 || onSheetChange) && (
        <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Abas do Excel">
          {sheets.map((item) => (
            <button
              key={item.name}
              type="button"
              onClick={() => onSheetChange?.(item.name)}
              aria-pressed={item.name === activeSheet}
              className={cn(
                'inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
                item.name === activeSheet
                  ? 'border-accent/40 bg-accent-soft/50 text-foreground'
                  : 'border-border bg-surface-2/60 text-muted hover:text-foreground',
              )}
            >
              <Table2 className="size-3.5" aria-hidden="true" />
              {item.name}
              <span className="text-[10px] text-muted-2">{SHEET_KIND_LABELS[item.kind] ?? item.kind}</span>
            </button>
          ))}
        </div>
      )}

      {sheetMeta ? (
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-2">
          <Badge variant="outline">{SHEET_KIND_LABELS[sheetMeta.kind] ?? sheetMeta.kind}</Badge>
          <span>
            {formatNumber(sheetMeta.rows)} linhas × {formatNumber(sheetMeta.columns)} colunas
          </span>
          <span>· páginas de origem: {sheetMeta.source_pages.join(', ') || '—'}</span>
        </div>
      ) : null}

      {!activeSheet ? (
        <EmptyBlock title="Nenhuma aba disponível" description="O resultado ainda não possui abas para pré-visualizar." />
      ) : preview.isPending ? (
        <LoadingBlock label="Carregando prévia da aba" />
      ) : preview.isError ? (
        <ErrorBlock message={explainError(preview.error)} onRetry={() => void preview.refetch()} />
      ) : !preview.data || preview.data.headers.length === 0 ? (
        <EmptyBlock title="Aba vazia" description="Esta aba não possui células de dados." />
      ) : (
        <>
          <div className="overflow-hidden rounded-xl border border-border">
            <Table>
              <TableHeader className="sticky top-0 z-10 bg-surface-2">
                <TableRow>
                  <TableHead className="w-12 text-right">#</TableHead>
                  {preview.data.headers.map((header, index) => (
                    <TableHead key={`${header}-${index}`} className="min-w-24">
                      {header || `Coluna ${index + 1}`}
                    </TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {preview.data.rows.map((row, rowIndex) => {
                  const absoluteRow = rowIndex + offset;
                  return (
                    <TableRow key={absoluteRow}>
                      <TableCell className="w-12 text-right font-mono text-[10px] text-muted-2 tabular-nums">
                        {absoluteRow + 1}
                      </TableCell>
                      {row.map((cell, columnIndex) => {
                        const address = cellAddress(absoluteRow, columnIndex);
                        const isSelected = selectedCell === address;
                        const numeric =
                          cell.type === 'integer' ||
                          cell.type === 'decimal' ||
                          cell.type === 'money' ||
                          cell.type === 'percentage';
                        const content = (
                          <span className={cn(numeric && 'font-mono tabular-nums', 'block min-w-0 break-words')}>
                            {cell.display || <span className="text-muted-2">—</span>}
                          </span>
                        );
                        return (
                          <TableCell
                            key={`${address}-${columnIndex}`}
                            className={cn(
                              'max-w-72',
                              isSelected && 'bg-accent-soft/40 outline outline-1 outline-accent/50',
                            )}
                          >
                            {onCellClick ? (
                              <button
                                type="button"
                                onClick={() =>
                                  onCellClick({ sheet: activeSheet, address, value: cell.display })
                                }
                                aria-label={`Célula ${address}: ${cell.display || 'vazia'}`}
                                className="block w-full text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                              >
                                {content}
                              </button>
                            ) : (
                              content
                            )}
                          </TableCell>
                        );
                      })}
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>

          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-2">
            <span>
              Linhas {formatNumber(from)}–{formatNumber(to)} de {formatNumber(totalRows)}
            </span>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setOffset(Math.max(0, offset - pageSize))}
                disabled={offset === 0 || preview.isFetching}
              >
                <ChevronLeft aria-hidden="true" />
                Anterior
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setOffset(offset + pageSize)}
                disabled={offset + pageSize >= totalRows || preview.isFetching}
              >
                Próxima
                <ChevronRight aria-hidden="true" />
              </Button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
