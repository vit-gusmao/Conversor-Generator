import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { AuditIssue, AuditSummary, AuthSession } from '@pdf2x/contracts';
import { AuditPage } from '@/pages/audit-page';
import { useAuthStore } from '@/stores/auth';
import { getAuditEvents, getAuditIssues, getAuditSummary } from '@/services/audit';
import { createAnonymousSession } from '@/services/auth';

vi.mock('@/services/auth', () => ({
  createAnonymousSession: vi.fn(),
  fetchCurrentUser: vi.fn(),
}));

vi.mock('@/services/audit', () => ({
  getAuditSummary: vi.fn(),
  getAuditIssues: vi.fn(),
  getAuditEvents: vi.fn(),
  getPageAudit: vi.fn(),
  getCellEvidence: vi.fn(),
  exportAuditFile: vi.fn(),
  buildAuditTextReport: vi.fn(() => 'relatório de teste'),
}));

vi.mock('@/services/conversions', () => ({
  uploadConversion: vi.fn(),
  listConversions: vi.fn(),
  getConversion: vi.fn(),
  getResult: vi.fn(),
  getPreview: vi.fn(),
  downloadExcel: vi.fn(),
  retryConversion: vi.fn(),
  cancelConversion: vi.fn(),
  deleteConversion: vi.fn(),
  conversionEventsUrl: vi.fn(() => '/api/v1/conversions/conv-1/events'),
}));

const now = new Date().toISOString();

const session: AuthSession = {
  access_token: 'token-test',
  token_type: 'Bearer',
  expires_in: 3600,
  user: {
    id: 'user-1',
    email: null,
    name: null,
    role: 'owner',
    is_anonymous: true,
    created_at: now,
  },
};

const issue: AuditIssue = {
  issue_id: 'issue-1',
  run_id: 'run-1',
  severity: 'warning',
  code: 'TABLE_LOW_CONFIDENCE',
  message: 'Tabela sem bordas reconstruída com baixa confiança na página 4',
  page_number: 4,
  region_id: null,
  context: {},
  created_at: now,
};

const summary: AuditSummary = {
  conversion_id: 'conv-1',
  run_id: 'run-1',
  quality: 'partial',
  status: 'completed_with_warnings',
  metrics: {
    pages_analyzed: 3,
    pages_total: 3,
    blocks_total: 18,
    blocks_assigned: 18,
    blocks_preserved: 2,
    blocks_unclassified: 0,
    blocks_dropped: 0,
    cells_generated: 32,
    tables_detected: 2,
    fields_detected: 4,
    records_detected: 6,
    ocr_regions: 1,
    events_total: 40,
    issues_total: 1,
    warnings: 1,
    errors: 0,
    coverage_percent: 98.5,
    assignment_percent: 100,
  },
  coverage: [],
  engines: [
    {
      engine: 'pdfplumber',
      kind: 'native',
      regions: 12,
      pages: 3,
      duration_ms: 420,
      accepted: 12,
      rejected: 0,
    },
  ],
  decisions: [],
  validations: [],
  timings: [
    { stage: 'extract', started_at: now, finished_at: now, duration_ms: 420, pages: 3 },
  ],
  hash_chain_valid: true,
  generated_at: now,
};

function renderAudit() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/conversions/conv-1/audit']}>
        <Routes>
          <Route path="/conversions/:id/audit" element={<AuditPage />} />
          <Route path="/conversions/:id" element={<div>Processamento</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useAuthStore.setState({ token: 'token-test', user: session.user, status: 'ready', error: null });
  vi.mocked(createAnonymousSession).mockResolvedValue(session);
  vi.mocked(getAuditSummary).mockResolvedValue(summary);
  vi.mocked(getAuditIssues).mockResolvedValue({ items: [issue], total: 1 });
  vi.mocked(getAuditEvents).mockResolvedValue({ items: [], total: 0, next_sequence: null });
});

describe('AuditPage', () => {
  it('renders all 12 audit tabs and the overview metrics', async () => {
    renderAudit();

    const tabNames = [
      'Visão Geral',
      'Linha do Tempo',
      'Páginas',
      'Campos',
      'Tabelas',
      'Dados Extraídos',
      'Excel Gerado',
      'Decisões',
      'Inconsistências',
      'Desempenho',
      'Auditoria Técnica',
      'Exportar Auditoria',
    ];

    for (const name of tabNames) {
      expect(await screen.findByRole('tab', { name })).toBeInTheDocument();
    }

    expect(await screen.findByText('Blocos atribuídos')).toBeInTheDocument();
    expect(screen.getByText('18 / 18')).toBeInTheDocument();
    expect(screen.getAllByText(/Cadeia de hashes válida/).length).toBeGreaterThan(0);
  });

  it('loads the summary and surfaces an issue in the inconsistencies tab', async () => {
    const user = userEvent.setup();
    renderAudit();

    expect(await screen.findByText('Blocos atribuídos')).toBeInTheDocument();
    expect(getAuditSummary).toHaveBeenCalledWith('conv-1');

    await user.click(await screen.findByRole('tab', { name: 'Inconsistências' }));

    expect(await screen.findByText(/agrupadas por gravidade/)).toBeInTheDocument();
    const matches = await screen.findAllByText(/Tabela sem bordas reconstruída/);
    expect(matches.length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('TABLE_LOW_CONFIDENCE')).toBeInTheDocument();
  });
});
