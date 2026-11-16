import { useMemo, useState } from 'react';
import type { AuditSummary } from '@pdf2x/contracts';
import { Search } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { EmptyBlock, ErrorBlock, LoadingBlock } from '@/components/layout/states';
import { useAggregatedPageAudits } from '@/hooks/use-audit';
import { useDebouncedValue } from '@/hooks/use-debounced-value';
import { explainError } from '@/lib/errors';
import { formatNumber, truncate } from '@/lib/format';
import { DESTINATION_LABELS, ORIGIN_LABELS } from '@/lib/labels';

export function ExtractedDataTab({
  conversionId,
  summary,
}: {
  conversionId: string;
  summary: AuditSummary;
}) {
  const [origin, setOrigin] = useState('all');
  const [search, setSearch] = useState('');
  const debounced = useDebouncedValue(search, 300);
  const pages = useAggregatedPageAudits(conversionId, summary.metrics.pages_total);

  const blocks = useMemo(
    () =>
      pages.data.flatMap((page) =>
        page.blocks.map((block) => ({ ...block, pageNumber: page.page_number })),
      ),
    [pages.data],
  );

  const filtered = useMemo(() => {
    const term = debounced.trim().toLowerCase();
    return blocks.filter((block) => {
      if (origin !== 'all' && block.origin !== origin) return false;
      if (term && !block.text.toLowerCase().includes(term)) return false;
      return true;
    });
  }, [blocks, origin, debounced]);

  const visible = filtered.slice(0, 400);

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle>Dados extraídos</CardTitle>
          <span className="text-xs text-muted-2">
            {formatNumber(filtered.length)} de {formatNumber(blocks.length)} blocos
          </span>
        </div>
      </CardHeader>
      <CardContent className="space-y-3 pt-0">
        <div className="flex flex-col gap-2 sm:flex-row">
          <div className="relative flex-1">
            <Search
              className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-2"
              aria-hidden="true"
            />
            <Input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Buscar texto extraído…"
              aria-label="Buscar texto extraído"
              className="pl-9"
            />
          </div>
          <div className="sm:w-52">
            <Select value={origin} onValueChange={setOrigin}>
              <SelectTrigger aria-label="Filtrar por origem">
                <SelectValue placeholder="Todas as origens" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">Todas as origens</SelectItem>
                <SelectItem value="native">Nativo</SelectItem>
                <SelectItem value="ocr">OCR</SelectItem>
                <SelectItem value="form_field">Campo de formulário</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>

        {pages.isLoading ? (
          <LoadingBlock label="Carregando dados extraídos" />
        ) : pages.error ? (
          <ErrorBlock message={explainError(pages.error)} onRetry={pages.refetch} />
        ) : blocks.length === 0 ? (
          <EmptyBlock
            title="Nenhum texto extraído"
            description="O motor não recuperou blocos textuais deste documento."
          />
        ) : filtered.length === 0 ? (
          <EmptyBlock
            title="Nenhum bloco corresponde aos filtros"
            description="Ajuste a busca ou escolha outra origem."
          />
        ) : (
          <>
            <div className="overflow-hidden rounded-xl border border-border">
              <Table>
                <TableHeader className="bg-surface-2">
                  <TableRow>
                    <TableHead>Texto</TableHead>
                    <TableHead>Origem</TableHead>
                    <TableHead className="text-right">Página</TableHead>
                    <TableHead>bbox</TableHead>
                    <TableHead>Destino</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {visible.map((block) => (
                    <TableRow key={`${block.pageNumber}-${block.element_id}`}>
                      <TableCell className="max-w-96">
                        <span title={block.text}>{truncate(block.text, 140)}</span>
                        <p className="mt-0.5 font-mono text-[10px] text-muted-2">{block.element_id}</p>
                      </TableCell>
                      <TableCell>
                        <Badge variant={block.origin === 'ocr' ? 'warning' : 'secondary'}>
                          {ORIGIN_LABELS[block.origin] ?? block.origin}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-right text-xs text-muted tabular-nums">
                        {block.pageNumber}
                      </TableCell>
                      <TableCell className="whitespace-nowrap font-mono text-[10px] text-muted-2">
                        [{block.bbox.map((value) => Math.round(value)).join(', ')}]
                      </TableCell>
                      <TableCell className="text-xs">
                        {block.destination ? (
                          <span className="inline-flex flex-wrap items-center gap-1.5">
                            <Badge variant="outline">
                              {DESTINATION_LABELS[block.destination.kind] ?? block.destination.kind}
                            </Badge>
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
            </div>
            {filtered.length > visible.length ? (
              <p className="text-[11px] text-warning">
                Exibindo os primeiros {formatNumber(visible.length)} blocos. Refine a busca para ver os demais.
              </p>
            ) : null}
          </>
        )}
      </CardContent>
    </Card>
  );
}
