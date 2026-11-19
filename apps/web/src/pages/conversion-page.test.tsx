import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { Conversion, ConversionEvent } from '@pdf2x/contracts';

import { ConversionPage } from '@/pages/conversion-page';
import { useConversionEvents } from '@/hooks/use-conversion-events';
import { useConversion } from '@/hooks/use-conversions';

vi.mock('@/hooks/use-session', () => ({
  useEnsureSession: vi.fn(),
}));

vi.mock('@/hooks/use-conversion-events', () => ({
  useConversionEvents: vi.fn(),
}));

vi.mock('@/hooks/use-conversions', () => ({
  useConversion: vi.fn(),
  useCancelConversion: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useRetryConversion: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
}));

/** Run 2 is the retry; the conversion record already shows it as queued. */
const RETRIED_CONVERSION = {
  conversion_id: 'conv-1',
  status: 'queued',
  stage: 'received',
  progress: 0,
  progress_indeterminate: false,
  quality: 'pending',
  document: {
    id: 'doc-1',
    filename: 'contrato.pdf',
    mime_type: 'application/pdf',
    size_bytes: 2048,
    sha256: 'a'.repeat(64),
    page_count: null,
    created_at: '2026-11-19T10:00:00Z',
  },
  run: { run_id: 'run-2', attempt: 2, started_at: null, finished_at: null },
  stats: null,
  validation: null,
  error: null,
  created_at: '2026-11-19T10:00:00Z',
  updated_at: '2026-11-19T10:00:00Z',
  links: {},
} as unknown as Conversion;

/** The replayed stream still carries the first attempt's terminal failure. */
const PREVIOUS_RUN_FAILURE: ConversionEvent = {
  sequence: 9,
  event_id: 'evt-9',
  conversion_id: 'conv-1',
  run_id: 'run-1',
  type: 'failed',
  status: 'failed',
  stage: 'failed',
  progress: 1,
  progress_indeterminate: false,
  message: 'falhou na primeira tentativa',
  payload: {},
  at: '2026-11-19T10:00:00Z',
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/conversions/conv-1']}>
        <Routes>
          <Route path="/conversions/:id" element={<ConversionPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(useConversion).mockReturnValue({
    isPending: false,
    isError: false,
    data: RETRIED_CONVERSION,
    refetch: vi.fn(),
  } as never);
  vi.mocked(useConversionEvents).mockReturnValue({
    events: [PREVIOUS_RUN_FAILURE],
    lastEvent: PREVIOUS_RUN_FAILURE,
    lastEventId: null,
    connection: 'open',
    transport: 'fetch',
    error: null,
    reconnectAttempts: 0,
  } as never);
});

describe('ConversionPage after a retry', () => {
  it('does not show the previous run failure while the new run is queued', () => {
    renderPage();

    expect(screen.queryByText('A conversão falhou')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /tentar novamente/i })).not.toBeInTheDocument();
    // The new run is still active, so it can be cancelled.
    expect(screen.getByRole('button', { name: /cancelar/i })).toBeInTheDocument();
  });

  it('subscribes to the events of the current run only', () => {
    renderPage();

    expect(useConversionEvents).toHaveBeenCalledWith(
      'conv-1',
      expect.objectContaining({ enabled: true, runId: 'run-2' }),
    );
  });
});
