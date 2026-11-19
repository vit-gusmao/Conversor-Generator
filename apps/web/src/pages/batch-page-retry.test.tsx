import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
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

import { getBatch, listBatchItems, retryItem } from '@/services/batches';
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
      total_items: 1,
      pending_upload: 0,
      uploading: 0,
      queued: 0,
      processing: 0,
      completed: 0,
      with_warnings: 0,
      failed: 0,
      cancelled: 1,
    },
    progress: {
      finished_items: 1,
      pages_total: 0,
      pages_attempted: 0,
      pages_completed: 0,
      pages_failed: 0,
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
    filename: 'contrato.pdf',
    size_bytes: 1024,
    kind: 'pdf',
    status: 'cancelled',
    options: defaultConversionOptions('pdf'),
    available_formats: [],
    upload_id: null,
    conversion_id: null,
    pages_total: null,
    pages_attempted: null,
    pages_completed: null,
    pages_failed: null,
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
          <Route path="/" element={<div>Página inicial</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function givenItems(items: BatchItem[]) {
  vi.mocked(getBatch).mockResolvedValue(makeBatch());
  vi.mocked(listBatchItems).mockResolvedValue({ items, next_cursor: null });
}

beforeEach(() => {
  useAuthStore.setState({ token: null, user: null, status: 'idle', error: null });
  vi.mocked(createAnonymousSession).mockResolvedValue(session);
  vi.mocked(getBatch).mockReset();
  vi.mocked(listBatchItems).mockReset();
  vi.mocked(retryItem).mockReset();
  vi.mocked(retryItem).mockResolvedValue({ item: makeItem() } as never);
});

describe('BatchPage retry', () => {
  it('retries a cancelled item without conversion_id with scope "upload"', async () => {
    givenItems([makeItem({ status: 'cancelled', conversion_id: null })]);
    renderBatchPage();

    const button = await screen.findByRole('button', { name: 'Tentar novamente contrato.pdf' });
    fireEvent.click(button);

    await waitFor(() => expect(retryItem).toHaveBeenCalledTimes(1));
    expect(retryItem).toHaveBeenCalledWith('batch-1', 'item-1', 'upload');
  });

  it('retries a failed item that has a conversion with scope "conversion"', async () => {
    givenItems([
      makeItem({ status: 'failed', conversion_id: 'conv-1', error: { code: 'X', message: 'falhou' } }),
    ]);
    renderBatchPage();

    fireEvent.click(await screen.findByRole('button', { name: 'Tentar novamente contrato.pdf' }));

    await waitFor(() => expect(retryItem).toHaveBeenCalledTimes(1));
    expect(retryItem).toHaveBeenCalledWith('batch-1', 'item-1', 'conversion');
  });

  it('does not offer a server retry for upload_failed and explains how to resend', async () => {
    givenItems([makeItem({ status: 'upload_failed', conversion_id: null })]);
    renderBatchPage();

    expect(await screen.findByText('contrato.pdf')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Tentar novamente contrato.pdf' })).not.toBeInTheDocument();
    expect(screen.getByText(/Envie o arquivo novamente pela tela inicial/)).toBeInTheDocument();
    expect(retryItem).not.toHaveBeenCalled();
  });
});
