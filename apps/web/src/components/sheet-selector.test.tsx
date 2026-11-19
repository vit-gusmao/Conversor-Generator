import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { SheetPage } from '@pdf2x/contracts';

import { SheetSelector, type SheetCatalog } from '@/components/sheet-selector';

vi.mock('@/services/results', () => ({
  getResultOverview: vi.fn(),
  listSheets: vi.fn(),
  getSheetPages: vi.fn(),
}));

import { listSheets } from '@/services/results';

function sheet(index: number) {
  return {
    id: `sheet_${index}`,
    name: `Aba ${index + 1}`,
    kind: 'records' as const,
    rows: 1,
    columns: 1,
    source_page_count: 1,
    table_id: null,
  };
}

/** 60 sheets: a full first page of 50 that points at a second page of 10, which ends the list. */
function pageFor(cursor: string | null | undefined): SheetPage {
  if (cursor === 'cursor-2') {
    return { items: Array.from({ length: 10 }, (_, index) => sheet(50 + index)), next_cursor: null, total: 60 };
  }
  return { items: Array.from({ length: 50 }, (_, index) => sheet(index)), next_cursor: 'cursor-2', total: 60 };
}

function renderSelector(onCatalogChange?: (catalog: SheetCatalog) => void) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <SheetSelector
        conversionId="conv-1"
        value="Aba 1"
        onChange={() => undefined}
        onCatalogChange={onCatalogChange}
      />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(listSheets).mockImplementation(async (_id, params) => pageFor(params?.cursor));
});

describe('SheetSelector pagination', () => {
  it('loads the second page once and hides "Mais" when the server has no more pages', async () => {
    renderSelector();

    fireEvent.click(await screen.findByRole('button', { name: /^mais$/i }));

    await waitFor(() => expect(screen.getByText('60 de 60')).toBeInTheDocument());
    const select = screen.getByRole('combobox', { name: /aba exibida/i });
    expect(within(select).getAllByRole('option')).toHaveLength(60);

    // The last page is exhausted: no button and no further request.
    expect(screen.queryByRole('button', { name: /^mais$/i })).not.toBeInTheDocument();
    expect(listSheets).toHaveBeenCalledTimes(2);
    expect(vi.mocked(listSheets).mock.calls[1]?.[1]).toMatchObject({ cursor: 'cursor-2' });
  });

  it('reports the loaded catalog as exhausted only after the last page', async () => {
    const onCatalogChange = vi.fn();
    renderSelector(onCatalogChange);

    fireEvent.click(await screen.findByRole('button', { name: /^mais$/i }));

    await waitFor(() =>
      expect(onCatalogChange).toHaveBeenLastCalledWith({
        names: expect.arrayContaining(['Aba 1', 'Aba 60']),
        exhausted: true,
      }),
    );
    const last = onCatalogChange.mock.calls.at(-1)?.[0] as SheetCatalog;
    expect(last.names).toHaveLength(60);
    // Before "Mais" was used the first page reported more pages to come.
    expect(onCatalogChange.mock.calls[0]?.[0]).toMatchObject({ exhausted: false });
  });
});
