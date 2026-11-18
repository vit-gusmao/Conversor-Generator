import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { Conversion, ResultOverview, SheetPage } from '@pdf2x/contracts';

import type { ConversionArtifact } from '@/services/conversions';
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

import {
  downloadExcel,
  downloadExport,
  getConversion,
  getPreview,
  listExports,
} from '@/services/conversions';
import { getResultOverview, listSheets } from '@/services/results';

const OVERVIEW: ResultOverview = {
  conversion_id: 'conv-1',
  run_id: 'run-1',
  status: 'completed',
  quality: 'validated',
  publication_status: 'approved',
  counts: { pages: 3, tables: 2, sheets: 3, cells: 120, records: 8, fields: 12 },
  available_formats: ['xlsx', 'txt', 'csv'],
  default_sheet: 'Todos os dados',
  warning_counts: { total: 1, error: 0, critical: 0, warning: 1 },
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
    filename: 'relatorio.pdf',
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

const SHEETS: SheetPage = {
  items: [
    {
      id: 'sheet_000',
      name: 'Todos os dados',
      kind: 'consolidated',
      rows: 9,
      columns: 5,
      source_page_count: 3,
      table_id: null,
    },
    {
      id: 'sheet_001',
      name: 'Registros',
      kind: 'records',
      rows: 4,
      columns: 3,
      source_page_count: 1,
      table_id: null,
    },
  ],
  next_cursor: null,
  total: 2,
};

const ARTIFACTS: { items: ConversionArtifact[]; total: number } = {
  total: 3,
  items: [
    {
      export_id: 'exp-xlsx',
      format: 'xlsx',
      filename: 'relatorio.xlsx',
      mime_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      sha256: 'b'.repeat(64),
      size_bytes: 4096,
      warnings: [],
      options: {},
      publication_status: 'approved',
    },
    {
      export_id: 'exp-txt',
      format: 'txt',
      filename: 'relatorio.txt',
      mime_type: 'text/plain',
      sha256: 'c'.repeat(64),
      size_bytes: 1024,
      warnings: [],
      options: {},
      publication_status: 'approved',
    },
  ],
};

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/conversions/conv-1/result']}>
        <Routes>
          <Route path="/conversions/:id" element={<div>Tela de processamento</div>} />
          <Route path="/conversions/:id/result" element={<ResultPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('ResultPage', () => {
  beforeEach(() => {
    useAuthStore.setState({
      token: 'token',
      user: null,
      status: 'ready',
      error: null,
    } as never);

    vi.mocked(getResultOverview).mockResolvedValue(OVERVIEW);
    vi.mocked(getConversion).mockResolvedValue(CONVERSION);
    vi.mocked(listSheets).mockResolvedValue(SHEETS);
    vi.mocked(listExports).mockResolvedValue(ARTIFACTS);
    vi.mocked(getPreview).mockResolvedValue({
      sheet: 'Todos os dados',
      headers: ['Aba', 'Página', 'Nome'],
      rows: [[{ value: 'Registros', type: 'text', display: 'Registros' }]],
      total_rows: 1,
      offset: 0,
      limit: 100,
    });
    vi.mocked(downloadExcel).mockResolvedValue(new Blob(['x']));
    vi.mocked(downloadExport).mockResolvedValue(new Blob(['t']));
  });

  it('shows a single sheet selector and a single preview table', async () => {
    renderPage();

    expect(await screen.findByRole('combobox', { name: /aba exibida/i })).toBeInTheDocument();
    expect(await screen.findAllByRole('table')).toHaveLength(1);

    // The old layout repeated every sheet as a button beside the preview.
    expect(screen.queryByRole('group', { name: /abas do excel/i })).not.toBeInTheDocument();
  });

  it('offers the workbook download when the artefact is published', async () => {
    renderPage();
    expect(await screen.findByRole('button', { name: /baixar excel/i })).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: /^texto$/i })).toBeInTheDocument();
  });

  it('does not offer Excel for a text-only result', async () => {
    vi.mocked(getResultOverview).mockResolvedValue({
      ...OVERVIEW,
      available_formats: ['txt'],
      counts: { ...OVERVIEW.counts, sheets: 0, cells: 0, tables: 0 },
    });
    vi.mocked(listSheets).mockResolvedValue({ items: [], next_cursor: null, total: 0 });
    vi.mocked(listExports).mockResolvedValue({
      total: 1,
      items: [ARTIFACTS.items[1]],
    });

    renderPage();

    expect(await screen.findByRole('button', { name: /baixar texto/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /baixar excel/i })).not.toBeInTheDocument();
    expect(
      screen.getByText(/este documento foi entregue apenas como texto/i),
    ).toBeInTheDocument();
  });

  it('keeps a partial result visible instead of hiding it in a menu', async () => {
    vi.mocked(getResultOverview).mockResolvedValue({
      ...OVERVIEW,
      quality: 'partial',
      partial: true,
      pages_failed: 2,
      pages_total: 10,
    });

    renderPage();

    expect(await screen.findByRole('alert')).toHaveTextContent(/resultado incompleto/i);
    expect(screen.getByRole('alert')).toHaveTextContent(/2 de 10 página/i);
  });

  it('blocks downloads when the artefact is in quarantine', async () => {
    vi.mocked(getResultOverview).mockResolvedValue({
      ...OVERVIEW,
      status: 'failed',
      publication_status: 'quarantined',
    });

    renderPage();

    expect(await screen.findByText(/artefato em quarentena/i)).toBeInTheDocument();
    const disabled = screen.getByRole('button', { name: /downloads indisponíveis/i });
    expect(disabled).toBeDisabled();
    expect(screen.queryByRole('button', { name: /baixar excel/i })).not.toBeInTheDocument();
  });

  it('keeps technical details collapsed behind a disclosure', async () => {
    renderPage();
    await screen.findByRole('combobox', { name: /aba exibida/i });

    const details = screen.getByText(/detalhes do documento/i);
    expect(details).toBeInTheDocument();
    // The run id is present in the DOM but not competing with the preview.
    expect(screen.getByText('run-1')).toBeInTheDocument();
  });

  it('renders only the first page of sheets for a huge workbook', async () => {
    const many: SheetPage = {
      items: Array.from({ length: 50 }, (_, index) => ({
        id: `sheet_${index}`,
        name: `Aba ${index + 1}`,
        kind: 'records' as const,
        rows: 10,
        columns: 4,
        source_page_count: 1,
        table_id: null,
      })),
      next_cursor: 'cursor-next',
      total: 1000,
    };
    vi.mocked(listSheets).mockResolvedValue(many);

    renderPage();

    const select = await screen.findByRole('combobox', { name: /aba exibida/i });
    const options = Array.from(select.querySelectorAll('option'));
    await waitFor(() => expect(options.length).toBe(50));
    // 1.000 sheets exist, but only the requested page is mounted.
    expect(options.length).toBeLessThan(100);
    expect(screen.getByRole('button', { name: /^mais$/i })).toBeInTheDocument();
  });
});
