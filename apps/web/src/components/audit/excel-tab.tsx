import { useEffect, useState } from 'react';
import { FileSpreadsheet, Search } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { ErrorBlock, EmptyBlock, LoadingBlock } from '@/components/layout/states';
import { SheetPreview } from '@/components/sheet-preview';
import { ProvenanceDrawer } from '@/components/provenance-drawer';
import { useResult } from '@/hooks/use-conversions';
import { explainError } from '@/lib/errors';
import { formatNumber } from '@/lib/format';
import { SHEET_KIND_LABELS } from '@/lib/labels';

export function ExcelTab({
  conversionId,
  sheet,
  cell,
  onSelectCell,
}: {
  conversionId: string;
  sheet: string | null;
  cell: string | null;
  onSelectCell: (sheet: string | null, cell: string | null, value?: string) => void;
}) {
  const result = useResult(conversionId);
  const [drawerOpen, setDrawerOpen] = useState(Boolean(cell));
  const [previewValue, setPreviewValue] = useState<string | null>(null);
  const [manualSheet, setManualSheet] = useState(sheet ?? '');
  const [manualCell, setManualCell] = useState(cell ?? '');

  useEffect(() => {
    setDrawerOpen(Boolean(cell));
  }, [cell]);

  useEffect(() => {
    setManualSheet(sheet ?? '');
    setManualCell(cell ?? '');
  }, [sheet, cell]);

  if (result.isPending) return <LoadingBlock label="Carregando estrutura do Excel" />;

  if (result.isError) {
    return (
      <ErrorBlock
        title="Não foi possível carregar o Excel gerado"
        message={explainError(result.error)}
        onRetry={() => void result.refetch()}
      />
    );
  }

  if (!result.data) {
    return (
      <EmptyBlock
        icon={<FileSpreadsheet className="size-6" aria-hidden="true" />}
        title="Nenhum Excel gerado"
        description="Esta execução não produziu um arquivo. A auditoria dos eventos permanece disponível nas outras abas."
      />
    );
  }

  const sheets = result.data.sheets;
  const activeSheet = sheet ?? sheets[0]?.name ?? null;

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="pb-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle>Abas do arquivo gerado</CardTitle>
            <span className="text-xs text-muted-2">
              {formatNumber(sheets.length)} abas · {formatNumber(result.data.cells)} células
            </span>
          </div>
        </CardHeader>
        <CardContent className="pt-0">
          {sheets.length === 0 ? (
            <EmptyBlock title="Workbook sem abas" />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Aba</TableHead>
                  <TableHead>Tipo</TableHead>
                  <TableHead className="text-right">Linhas</TableHead>
                  <TableHead className="text-right">Colunas</TableHead>
                  <TableHead>Páginas de origem</TableHead>
                  <TableHead>Tabela</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {sheets.map((item) => (
                  <TableRow key={item.name}>
                    <TableCell>
                      <button
                        type="button"
                        onClick={() => onSelectCell(item.name, null)}
                        className="font-medium text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                      >
                        {item.name}
                      </button>
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline">{SHEET_KIND_LABELS[item.kind] ?? item.kind}</Badge>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{formatNumber(item.rows)}</TableCell>
                    <TableCell className="text-right tabular-nums">{formatNumber(item.columns)}</TableCell>
                    <TableCell className="text-xs text-muted">{item.source_pages.join(', ') || '—'}</TableCell>
                    <TableCell className="font-mono text-[11px] text-muted-2">
                      {item.table_id ?? '—'}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle>Explorador de células</CardTitle>
            <span className="text-xs text-muted-2">Clique em uma célula para ver a proveniência</span>
          </div>
        </CardHeader>
        <CardContent className="space-y-4 pt-0">
          <SheetPreview
            conversionId={conversionId}
            sheet={activeSheet}
            sheets={sheets}
            selectedCell={cell}
            onSheetChange={(name) => onSelectCell(name, null)}
            onCellClick={({ sheet: clickedSheet, address, value }) => {
              setPreviewValue(value);
              onSelectCell(clickedSheet, address, value);
            }}
          />

          <form
            className="flex flex-col gap-2 rounded-xl border border-border bg-surface-2/30 p-3 sm:flex-row sm:items-end"
            onSubmit={(event) => {
              event.preventDefault();
              if (!manualSheet.trim() || !manualCell.trim()) return;
              onSelectCell(manualSheet.trim(), manualCell.trim().toUpperCase());
            }}
          >
            <div className="flex-1 space-y-1">
              <label htmlFor="manual-sheet" className="text-[11px] uppercase tracking-wide text-muted-2">
                Aba
              </label>
              <Input
                id="manual-sheet"
                value={manualSheet}
                onChange={(event) => setManualSheet(event.target.value)}
                placeholder="Dados"
              />
            </div>
            <div className="w-full space-y-1 sm:w-40">
              <label htmlFor="manual-cell" className="text-[11px] uppercase tracking-wide text-muted-2">
                Célula
              </label>
              <Input
                id="manual-cell"
                value={manualCell}
                onChange={(event) => setManualCell(event.target.value)}
                placeholder="B2"
                className="font-mono"
              />
            </div>
            <Button type="submit" variant="secondary" disabled={!manualSheet.trim() || !manualCell.trim()}>
              <Search aria-hidden="true" />
              Investigar célula
            </Button>
          </form>
        </CardContent>
      </Card>

      <ProvenanceDrawer
        open={drawerOpen}
        onOpenChange={(open) => {
          setDrawerOpen(open);
          if (!open) onSelectCell(sheet, null);
        }}
        conversionId={conversionId}
        sheet={sheet}
        cell={cell}
        previewValue={previewValue}
      />
    </div>
  );
}
