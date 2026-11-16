import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { AuthSession, PlatformSettings } from '@pdf2x/contracts';
import { HomePage } from '@/pages/home-page';
import { useAuthStore } from '@/stores/auth';

vi.mock('@/services/settings', () => ({
  getPlatformSettings: vi.fn(),
}));

vi.mock('@/services/auth', () => ({
  createAnonymousSession: vi.fn(),
  fetchCurrentUser: vi.fn(),
}));

vi.mock('@/services/conversions', () => ({
  uploadConversion: vi.fn(),
  listConversions: vi.fn(),
  getCapabilities: vi.fn(),
  listExports: vi.fn(),
  createExport: vi.fn(),
  downloadExport: vi.fn(),
  getConversion: vi.fn(),
  getResult: vi.fn(),
  getPreview: vi.fn(),
  getTextPage: vi.fn(),
  downloadExcel: vi.fn(),
  retryConversion: vi.fn(),
  cancelConversion: vi.fn(),
  deleteConversion: vi.fn(),
  conversionEventsUrl: vi.fn(() => '/api/v1/conversions/conv-1/events'),
}));

import { getPlatformSettings } from '@/services/settings';
import { getCapabilities, uploadConversion, listConversions } from '@/services/conversions';
import { createAnonymousSession } from '@/services/auth';

const settings: PlatformSettings = {
  auth_mode: 'optional',
  max_upload_bytes: 10 * 1024 * 1024,
  max_pages: 500,
  runtime_mode: 'standalone',
  ocr_enabled: true,
  ocr_engine: 'rapidocr',
  storage_backend: 'local',
  retention: { pdf_days: 7, xlsx_days: 30, audit_days: 90 },
  engine_version: '1.0.0',
  capabilities: { ocr: true, tables: true },
};

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
    created_at: new Date().toISOString(),
  },
};

function renderHome() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/conversions/:id" element={<div>Página de conversão</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useAuthStore.setState({ token: null, user: null, status: 'idle', error: null });
  vi.mocked(getPlatformSettings).mockResolvedValue(settings);
  vi.mocked(createAnonymousSession).mockResolvedValue(session);
  vi.mocked(getCapabilities).mockResolvedValue({
    inputs: [],
    outputs: [
      { format: 'xlsx', mime_type: '', extension: '.xlsx', description: 'Planilha', available: true },
      { format: 'txt', mime_type: '', extension: '.txt', description: 'Texto', available: true },
      { format: 'csv', mime_type: '', extension: '.csv', description: 'Tabelas', available: true },
    ],
    ocr: { available: true },
    limits: { max_bytes: 10 * 1024 * 1024, max_pages: 500 },
  });
  vi.mocked(listConversions).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 5 });
  vi.mocked(uploadConversion).mockReset();
});

describe('HomePage', () => {
  it('rejects an unsupported file with an accessible error and never calls the API', async () => {
    const user = userEvent.setup({ applyAccept: false });
    renderHome();

    const input = screen.getByLabelText('Selecionar PDF ou imagem');
    await user.upload(input, new File(['hello'], 'documento.txt', { type: 'text/plain' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/PDF|imagem/i);
    expect(uploadConversion).not.toHaveBeenCalled();
  });

  it('accepts a PDF, shows the selected file and navigates to the conversion', async () => {
    const user = userEvent.setup();
    vi.mocked(uploadConversion).mockResolvedValue({
      conversion_id: 'conv-1',
      status: 'queued',
      message: 'Recebido',
      links: {},
    });

    renderHome();

    const input = screen.getByLabelText('Selecionar PDF ou imagem');
    await user.upload(input, new File(['%PDF-1.4'], 'relatorio.pdf', { type: 'application/pdf' }));

    expect(await screen.findByText('relatorio.pdf')).toBeInTheDocument();
    const submit = await screen.findByRole('button', { name: /^Converter$/i });
    await user.click(submit);

    await waitFor(() => {
      expect(uploadConversion).toHaveBeenCalledTimes(1);
    });
    const call = vi.mocked(uploadConversion).mock.calls[0];
    expect(call?.[0]?.name).toBe('relatorio.pdf');
    expect(call?.[1]?.target_formats).toEqual(['xlsx', 'txt', 'csv']);

    expect(await screen.findByText('Página de conversão')).toBeInTheDocument();
  });
});
