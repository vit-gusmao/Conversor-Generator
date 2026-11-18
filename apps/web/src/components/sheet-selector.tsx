import { useEffect, useState } from 'react';
import type { SheetDescriptor } from '@pdf2x/contracts';
import { ChevronDown, Loader2, Search, Table2 } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { useDebouncedValue } from '@/hooks/use-debounced-value';
import { useSheets } from '@/hooks/use-results';
import { explainError } from '@/lib/errors';
import { formatNumber } from '@/lib/format';
import { SHEET_KIND_LABELS } from '@/lib/labels';
import { listSheets } from '@/services/results';

export interface SheetSelectorProps {
  conversionId: string;
  value: string | null;
  onChange: (sheetName: string) => void;
  /** Rows per page of the selector; a transport parameter, not a limit. */
  pageSize?: number;
}

/**
 * One searchable selector for every sheet.
 *
 * A workbook can have thousands of sheets: they are paged in from the server
 * instead of being rendered as thousands of buttons. The native `<select>`
 * keeps keyboard and screen-reader behaviour for free.
 */
export function SheetSelector({ conversionId, value, onChange, pageSize = 50 }: SheetSelectorProps) {
  const [search, setSearch] = useState('');
  const debounced = useDebouncedValue(search, 300);
  const query = useSheets(conversionId, { q: debounced, limit: pageSize });

  const [extra, setExtra] = useState<SheetDescriptor[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<string | null>(null);

  // A new search (or a refetch of the first page) invalidates the appended pages.
  useEffect(() => {
    setExtra([]);
    setCursor(null);
    setMoreError(null);
  }, [debounced, conversionId]);

  const firstPage = query.data?.items ?? [];
  const items = [...firstPage, ...extra];
  const nextCursor = cursor ?? query.data?.next_cursor ?? null;
  const total = query.data?.total ?? items.length;

  async function loadMore() {
    if (!nextCursor || loadingMore) return;
    setLoadingMore(true);
    setMoreError(null);
    try {
      const page = await listSheets(conversionId, {
        q: debounced,
        cursor: nextCursor,
        limit: pageSize,
      });
      setExtra((current) => [...current, ...page.items]);
      setCursor(page.next_cursor);
    } catch (cause) {
      setMoreError(explainError(cause));
    } finally {
      setLoadingMore(false);
    }
  }

  return (
    <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
      <div className="relative min-w-0 flex-1">
        <label className="sr-only" htmlFor="sheet-search">
          Buscar aba
        </label>
        <Search
          className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-2"
          aria-hidden="true"
        />
        <Input
          id="sheet-search"
          type="search"
          value={search}
          placeholder="Buscar aba…"
          className="pl-8"
          onChange={(event) => setSearch(event.target.value)}
        />
      </div>

      <div className="flex min-w-0 items-center gap-2">
        <label className="sr-only" htmlFor="sheet-select">
          Aba exibida
        </label>
        <div className="relative min-w-0">
          <select
            id="sheet-select"
            className="h-9 w-full min-w-[12rem] max-w-[22rem] appearance-none truncate rounded-lg border border-border bg-surface-2 py-1.5 pl-3 pr-8 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            value={value ?? ''}
            onChange={(event) => onChange(event.target.value)}
            disabled={query.isPending || items.length === 0}
          >
            {items.length === 0 ? <option value="">Nenhuma aba</option> : null}
            {items.map((sheet) => (
              <option key={sheet.id} value={sheet.name}>
                {sheet.name} · {SHEET_KIND_LABELS[sheet.kind] ?? sheet.kind} ·{' '}
                {formatNumber(sheet.rows)} linhas
              </option>
            ))}
          </select>
          <ChevronDown
            className="pointer-events-none absolute right-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-2"
            aria-hidden="true"
          />
        </div>

        {nextCursor ? (
          <Button variant="ghost" size="sm" onClick={() => void loadMore()} disabled={loadingMore}>
            {loadingMore ? (
              <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
            ) : (
              <Table2 aria-hidden="true" />
            )}
            Mais
          </Button>
        ) : null}
      </div>

      {query.isError ? (
        <span className="text-xs text-error" role="alert">
          {explainError(query.error)}
        </span>
      ) : moreError ? (
        <span className="text-xs text-error" role="alert">
          {moreError}
        </span>
      ) : (
        <span className="text-xs text-muted-2" aria-live="polite">
          {formatNumber(items.length)} de {formatNumber(total)}
        </span>
      )}
    </div>
  );
}
