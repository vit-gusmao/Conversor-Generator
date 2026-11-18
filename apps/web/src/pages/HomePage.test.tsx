import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { AuthSession, BatchItem, BatchSummary, PlatformSettings } from '@pdf2x/contracts';
import { defaultConversionOptions } from '@pdf2x/contracts';
import { HomePage } from '@/pages/home-page';
import { useAuthStore } from '@/stores/auth';
import { useUploadQueue } from '@/stores/upload-queue';

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

import { getPlatformSettings } from '@/services/settings';
import { getCapabilities, listConversions } from '@/services/conversions';
import { createAnonymousSession } from '@/services/auth';
import { createBatch, registerItem } from '@/services/batches';
import { completeUpload, putPart } from '@/services/uploads';

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

function pdf(name: string, content = '%PDF-1.4'): File {
  return new File([content], name, { type: 'application/pdf' });
}

function image(name: string): File {
  return new File(['png'], name, { type: 'image/png' });
}

function makeBatchSummary(): BatchSummary {
  return {
    batch_id: 'batch-1',
    status: 'accepting',
    client_batch_key: 'key-1',
    counts: {
      total_items: 2,
      pending_upload: 2,
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
    created_at: new Date().toISOString(),
    sealed_at: null,
    cancel_requested_at: null,
    version: 1,
    links: { self: '', items: '', events: '', seal: '', cancel: '' },
  };
}

function makeBatchItem(): BatchItem {
  return {
    item_id: 'item-1',
    batch_id: 'batch-1',
    client_item_id: 'client-1',
    ordinal: 1,
    filename: 'a.pdf',
    size_bytes: 8,
    kind: 'pdf',
    status: 'pending_upload',
    options: defaultConversionOptions('pdf'),
    available_formats: ['xlsx', 'txt', 'csv'],
    upload_id: 'upload-1',
    conversion_id: null,
    pages_total: null,
    pages_attempted: null,
    pages_completed: null,
    pages_failed: null,
    phase: null,
    error: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    links: {},
  };
}

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
          <Route path="/batches/:id" element={<div>Página do lote</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useAuthStore.setState({ token: null, user: null, status: 'idle', error: null });
  useUploadQueue.getState().reset();

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

  vi.mocked(createBatch).mockReset();
  vi.mocked(registerItem).mockReset();
  vi.mocked(putPart).mockReset();
  vi.mocked(completeUpload).mockReset();
});

describe('HomePage', () => {
  it('queues two PDFs and one image as three items', async () => {
    const user = userEvent.setup();
    renderHome();

    const input = screen.getByLabelText('Selecionar PDF ou imagem');
    expect(input).toHaveAttribute('multiple');

    await user.upload(input, [pdf('a.pdf'), pdf('b.pdf'), image('c.png')]);

    expect(await screen.findByText('3 arquivos na fila')).toBeInTheDocument();
    expect(screen.getByText('a.pdf')).toBeInTheDocument();
    expect(screen.getByText('b.pdf')).toBeInTheDocument();
    expect(screen.getByText('c.png')).toBeInTheDocument();
    expect(useUploadQueue.getState().items.map((item) => item.kind)).toEqual([
      'pdf',
      'pdf',
      'image',
    ]);
  });

  it('accumulates a dropped file instead of replacing the queue', async () => {
    const user = userEvent.setup();
    renderHome();

    await user.upload(screen.getByLabelText('Selecionar PDF ou imagem'), [pdf('a.pdf')]);
    expect(await screen.findByText('1 arquivo na fila')).toBeInTheDocument();

    fireEvent.drop(screen.getByTestId('home-dropzone'), {
      dataTransfer: { files: [pdf('b.pdf')] },
    });

    expect(await screen.findByText('2 arquivos na fila')).toBeInTheDocument();
    expect(screen.getByText('a.pdf')).toBeInTheDocument();
    expect(screen.getByText('b.pdf')).toBeInTheDocument();
  });

  it('shows an error for an invalid file without clearing the queue', async () => {
    const user = userEvent.setup({ applyAccept: false });
    renderHome();

    const input = screen.getByLabelText('Selecionar PDF ou imagem');
    await user.upload(input, [pdf('valido.pdf')]);
    expect(await screen.findByText('1 arquivo na fila')).toBeInTheDocument();

    await user.upload(
      input,
      new File(['conteudo'], 'contrato.docx', {
        type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      }),
    );

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/PDF|imagem/i);
    expect(screen.getByText('valido.pdf')).toBeInTheDocument();
    expect(screen.getByText('1 arquivo na fila')).toBeInTheDocument();
  });

  it('shows the "Enviar arquivos" button disabled with an empty queue', () => {
    renderHome();

    const send = screen.getByRole('button', { name: /Enviar arquivos/i });
    expect(send).toBeInTheDocument();
    expect(send).toBeDisabled();
  });

  it('creates one batch, registers and uploads every queued file, then navigates', async () => {
    const user = userEvent.setup();
    vi.mocked(createBatch).mockResolvedValue(makeBatchSummary());
    vi.mocked(registerItem).mockResolvedValue({
      item: makeBatchItem(),
      upload_id: 'upload-1',
      part_size: 8 * 1024 * 1024,
      next_part: 0,
      replayed: false,
    });
    vi.mocked(putPart).mockResolvedValue({
      upload_id: 'upload-1',
      part_number: 0,
      size: 8,
      sha256: 'a'.repeat(64),
      received_bytes: 8,
      parts_received: 1,
      next_part: 1,
      replayed: false,
    });
    vi.mocked(completeUpload).mockResolvedValue({
      upload_id: 'upload-1',
      item_id: 'item-1',
      conversion_id: 'conv-1',
      status: 'queued',
      message: '',
      replayed: false,
    });

    renderHome();

    await user.upload(screen.getByLabelText('Selecionar PDF ou imagem'), [
      pdf('a.pdf'),
      pdf('b.pdf'),
    ]);
    expect(await screen.findByText('2 arquivos na fila')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /Enviar arquivos/i }));

    expect(await screen.findByText('Página do lote')).toBeInTheDocument();
    expect(createBatch).toHaveBeenCalledTimes(1);
    expect(registerItem).toHaveBeenCalledTimes(2);
    expect(putPart).toHaveBeenCalledTimes(2);
    expect(completeUpload).toHaveBeenCalledTimes(2);

    const registration = vi.mocked(registerItem).mock.calls[0];
    expect(registration?.[0]).toBe('batch-1');
    expect(registration?.[1]?.filename).toBe('a.pdf');
    expect(registration?.[1]?.options?.target_formats).toEqual(['xlsx', 'txt', 'csv']);
  });
});
