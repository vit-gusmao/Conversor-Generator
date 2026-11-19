import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { Conversion, ResultOverview, SheetPage } from '@pdf2x/contracts';

import { ResultPage } from '@/pages/result-page';
import { useAuthStore } from '@/stores/auth';

vi.mock('@/services/conversions', () => ({
  getConversion: vi.fn(),
  getPreview: vi.fn(),
  listExports: vi.fn(),
  downloadExcel: vi.fn(),
  downloadExport: vi.fn(),
  createExport: vi.fn(),
  uploadConversion: vi.fn(),
  listConversions: vi.fn(),
  getResult: vi.fn(),
  retryConversion: vi.fn(),
  cancelConversion: vi.fn(),
  deleteConversion: vi.fn(),
  getCapabilities: vi.fn(),
  getTextPage: vi.fn(),
  conversionEventsUrl: vi.fn(),
}));

vi.mock('@/services/results', () => ({
  getResultOverview: vi.fn(),
  listSheets: vi.fn(),
  getSheetPages: vi.fn(),
}));

import { getConversion, getPreview, listExports } from '@/services/conversions';
import { getResultOverview, listSheets } from '@/services/results';

const OVERVIEW: ResultOverview = {
  conversion_id: 'conv-1',
  run_id: 'run-1',
  status: 'completed',
  quality: 'validated',
  publication_status: 'approved',
  counts: { pages: 3, tables: 60, sheets: 60, cells: 600, records: 60, fields: 12 },
  available_formats: ['xlsx', 'txt', 'csv'],
  default_sheet: 'Aba 1',
  warning_counts: { total: 0, error: 0, critical: 0, warning: 0 },
  partial: false,
  pages_failed: 0,
  pages_total: 3,
  download_url: '/api/v1/conversions/conv-1/download',
  audit_url: '/api/v1/conversions/conv-1/audit',
};

const CONVERSION = {
  conversion_id: 'conv-1',
  status: 'completed',
  stage: 'done',
  progress: 1,
  progress_indeterminate: false,
  quality: 'validated',
  document: {
    id: 'doc-1',
    filename: 'planilha.pdf',
    mime_type: 'application/pdf',
    size_bytes: 2048,
    sha256: 'a'.repeat(64),
    page_count: 3,
    created_at: '2026-11-17T10:00:00Z',
  },
  run: { run_id: 'run-1', attempt: 1, started_at: null, finished_at: null },
  stats: null,
  validation: null,
  error: null,
  created_at: '2026-11-17T10:00:00Z',
  updated_at: '2026-11-17T10:00:00Z',
  links: {},
} as unknown as Conversion;

function sheet(index: number) {
  return {
    id: `sheet_${index}`,
    name: `Aba ${index + 1}`,
    kind: 'records' as const,
    rows: 2,
    columns: 2,
    source_page_count: 1,
    table_id: null,
  };
}

function pageFor(cursor: string | null | undefined): SheetPage {
  if (cursor === 'cursor-2') {
    return { items: Array.from({ length: 10 }, (_, index) => sheet(50 + index)), next_cursor: null, total: 60 };
  }
  return { items: Array.from({ length: 50 }, (_, index) => sheet(index)), next_cursor: 'cursor-2', total: 60 };
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/conversions/conv-1/result']}>
        <Routes>
          <Route path="/conversions/:id/result" element={<ResultPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useAuthStore.setState({ token: 'token', user: null, status: 'ready', error: null } as never);
  vi.mocked(getResultOverview).mockResolvedValue(OVERVIEW);
  vi.mocked(getConversion).mockResolvedValue(CONVERSION);
  vi.mocked(listSheets).mockImplementation(async (_id, params) => pageFor(params?.cursor));
  vi.mocked(listExports).mockResolvedValue({ items: [], total: 0 });
  vi.mocked(getPreview).mockResolvedValue({
    sheet: 'Aba 1',
    headers: ['Campo'],
    rows: [],
    total_rows: 0,
    offset: 0,
    limit: 100,
  });
});

describe('ResultPage sheet selection', () => {
  it('keeps a sheet chosen from a later page instead of resetting to the default', async () => {
    renderPage();

    const select = await screen.findByRole('combobox', { name: /aba exibida/i });
    fireEvent.click(await screen.findByRole('button', { name: /^mais$/i }));
    await waitFor(() => expect(within(select).getAllByRole('option')).toHaveLength(60));

    // "Aba 55" is on the second page, so it is absent from the first page of 50.
    fireEvent.change(select, { target: { value: 'Aba 55' } });

    // Let any reset effect run; the selection must survive it.
    await waitFor(() => expect(select).toHaveValue('Aba 55'));
    await waitFor(() =>
      expect(getPreview).toHaveBeenLastCalledWith(
        'conv-1',
        expect.objectContaining({ sheet: 'Aba 55' }),
      ),
    );
    expect(select).toHaveValue('Aba 55');
  });
});
