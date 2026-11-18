import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { AuthSession, BatchItem, BatchSummary } from '@pdf2x/contracts';
import { defaultConversionOptions } from '@pdf2x/contracts';
import { BatchPage } from '@/pages/batch-page';
import { useAuthStore } from '@/stores/auth';

vi.mock('@/services/batches', () => ({
  createBatch: vi.fn(),
  getBatch: vi.fn(),
  registerItem: vi.fn(),
  listBatchItems: vi.fn(),
  sealBatch: vi.fn(),
  cancelBatch: vi.fn(),
  cancelItem: vi.fn(),
  retryItem: vi.fn(),
  batchEventsUrl: vi.fn(() => '/api/v1/batches/batch-1/events'),
}));

vi.mock('@/services/uploads', () => ({
  putPart: vi.fn(),
  getUpload: vi.fn(),
  completeUpload: vi.fn(),
  abortUpload: vi.fn(),
}));

vi.mock('@/lib/sha256', () => ({
  sha256Hex: vi.fn().mockResolvedValue('a'.repeat(64)),
}));

vi.mock('@/services/auth', () => ({
  createAnonymousSession: vi.fn(),
  fetchCurrentUser: vi.fn(),
}));

vi.mock('@/hooks/use-batch-events', () => ({
  useBatchEvents: vi.fn(() => ({
    events: [],
    lastEvent: null,
    lastEventId: null,
    connection: 'idle' as const,
    transport: null,
    error: null,
    reconnectAttempts: 0,
  })),
  isTerminalBatchStatus: (status: string) =>
    ['completed', 'completed_with_errors', 'cancelled'].includes(status),
}));

import { getBatch, listBatchItems } from '@/services/batches';
import { createAnonymousSession } from '@/services/auth';

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

function makeBatch(overrides: Partial<BatchSummary> = {}): BatchSummary {
  return {
    batch_id: 'batch-1',
    status: 'accepting',
    client_batch_key: 'key-1',
    counts: {
      total_items: 2,
      pending_upload: 0,
      uploading: 0,
      queued: 0,
      processing: 0,
      completed: 1,
      with_warnings: 0,
      failed: 1,
      cancelled: 0,
    },
    progress: {
      finished_items: 2,
      pages_total: 12,
      pages_attempted: 12,
      pages_completed: 10,
      pages_failed: 2,
    },
    created_at: '2026-01-02T10:00:00.000Z',
    sealed_at: null,
    cancel_requested_at: null,
    version: 1,
    links: { self: '', items: '', events: '', seal: '', cancel: '' },
    ...overrides,
  };
}

function makeItem(overrides: Partial<BatchItem> = {}): BatchItem {
  return {
    item_id: 'item-1',
    batch_id: 'batch-1',
    client_item_id: 'client-1',
    ordinal: 1,
    filename: 'relatorio.pdf',
    size_bytes: 1024,
    kind: 'pdf',
    status: 'completed',
    options: defaultConversionOptions('pdf'),
    available_formats: ['xlsx', 'txt', 'csv'],
    upload_id: 'upload-1',
    conversion_id: 'conv-1',
    pages_total: 6,
    pages_attempted: 6,
    pages_completed: 6,
    pages_failed: 0,
    phase: null,
    error: null,
    created_at: '2026-01-02T10:00:00.000Z',
    updated_at: '2026-01-02T10:01:00.000Z',
    links: {},
    ...overrides,
  };
}

function renderBatchPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/batches/batch-1']}>
        <Routes>
          <Route path="/batches/:id" element={<BatchPage />} />
          <Route path="/conversions/:id/result" element={<div>Página de resultado</div>} />
          <Route path="/" element={<div>Página inicial</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useAuthStore.setState({ token: null, user: null, status: 'idle', error: null });
  vi.mocked(createAnonymousSession).mockResolvedValue(session);
  vi.mocked(getBatch).mockReset();
  vi.mocked(listBatchItems).mockReset();
});

describe('BatchPage', () => {
  it('renders the batch summary counts and the items', async () => {
    vi.mocked(getBatch).mockResolvedValue(makeBatch());
    vi.mocked(listBatchItems).mockResolvedValue({
      items: [
        makeItem(),
        makeItem({
          item_id: 'item-2',
          client_item_id: 'client-2',
          filename: 'quebrado.pdf',
          status: 'failed',
          conversion_id: null,
          error: { code: 'EXTRACTION_FAILED', message: 'Falha ao extrair texto do PDF.' },
        }),
      ],
      next_cursor: null,
    });

    renderBatchPage();

    expect(await screen.findByText('relatorio.pdf')).toBeInTheDocument();
    expect(screen.getByText('quebrado.pdf')).toBeInTheDocument();
    expect(screen.getByText('Resumo do lote')).toBeInTheDocument();
    expect(screen.getByText('Concluídos')).toBeInTheDocument();
    expect(screen.getByText('Falhas')).toBeInTheDocument();
    expect(screen.getByText(/10 de 12/)).toBeInTheDocument();
  });

  it('shows "Abrir resultado" only for items with a conversion_id', async () => {
    vi.mocked(getBatch).mockResolvedValue(makeBatch());
    vi.mocked(listBatchItems).mockResolvedValue({
      items: [
        makeItem(),
        makeItem({
          item_id: 'item-2',
          client_item_id: 'client-2',
          filename: 'quebrado.pdf',
          status: 'failed',
          conversion_id: null,
          error: { code: 'EXTRACTION_FAILED', message: 'Falha ao extrair texto do PDF.' },
        }),
      ],
      next_cursor: null,
    });

    renderBatchPage();

    const links = await screen.findAllByRole('link', { name: /Abrir resultado/i });
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute('href', '/conversions/conv-1/result');
  });

  it('disables sealing and explains why while an upload is pending', async () => {
    vi.mocked(getBatch).mockResolvedValue(
      makeBatch({
        counts: {
          total_items: 1,
          pending_upload: 1,
          uploading: 0,
          queued: 0,
          processing: 0,
          completed: 0,
          with_warnings: 0,
          failed: 0,
          cancelled: 0,
        },
        progress: {
          finished_items: 0,
          pages_total: 0,
          pages_attempted: 0,
          pages_completed: 0,
          pages_failed: 0,
        },
      }),
    );
    vi.mocked(listBatchItems).mockResolvedValue({
      items: [
        makeItem({
          status: 'pending_upload',
          conversion_id: null,
          pages_total: null,
          pages_attempted: null,
          pages_completed: null,
          pages_failed: null,
        }),
      ],
      next_cursor: null,
    });

    renderBatchPage();

    const seal = await screen.findByRole('button', { name: /Selar lote/i });
    expect(seal).toBeDisabled();
    expect(screen.getByText(/antes de selar o lote/i)).toBeInTheDocument();
  });

  it('shows the error message of a failed item', async () => {
    vi.mocked(getBatch).mockResolvedValue(
      makeBatch({
        counts: {
          total_items: 1,
          pending_upload: 0,
          uploading: 0,
          queued: 0,
          processing: 0,
          completed: 0,
          with_warnings: 0,
          failed: 1,
          cancelled: 0,
        },
      }),
    );
    vi.mocked(listBatchItems).mockResolvedValue({
      items: [
        makeItem({
          status: 'failed',
          conversion_id: null,
          error: {
            code: 'EXTRACTION_FAILED',
            message: 'Falha ao extrair texto do PDF.',
          },
        }),
      ],
      next_cursor: null,
    });

    renderBatchPage();

    expect(await screen.findByText('Falha ao extrair texto do PDF.')).toBeInTheDocument();
  });
});
