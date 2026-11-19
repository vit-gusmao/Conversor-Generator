import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { AutomationEvent, AutomationRunRecord } from '@pdf2x/contracts';
import { ScheduleRunPage } from '@/pages/schedule-run-page';

vi.mock('@/hooks/use-session', () => ({
  useEnsureSession: () => ({ ready: true }),
}));

vi.mock('@/services/schedules', () => ({
  listSchedules: vi.fn(),
  getSchedule: vi.fn(),
  createSchedule: vi.fn(),
  previewSchedule: vi.fn(),
  updateSchedule: vi.fn(),
  archiveSchedule: vi.fn(),
  pauseSchedule: vi.fn(),
  resumeSchedule: vi.fn(),
  runNow: vi.fn(),
  listRuns: vi.fn(),
  getRun: vi.fn(),
  getRunEvents: vi.fn(),
  cancelRun: vi.fn(),
}));

import * as schedulesService from '@/services/schedules';

const mockGetRun = schedulesService.getRun as unknown as ReturnType<typeof vi.fn>;
const mockGetRunEvents = schedulesService.getRunEvents as unknown as ReturnType<typeof vi.fn>;

function run(overrides: Partial<AutomationRunRecord> = {}): AutomationRunRecord {
  return {
    id: 'run_1',
    owner_id: 'owner',
    schedule_id: 'sch_1',
    schedule_revision: 1,
    scheduled_for: '2026-11-19T11:00:00+00:00',
    trigger: 'schedule',
    occurrence_key: 'sch_1:1:2026-11-19T11:00:00Z',
    state: 'running',
    phase: 'convert',
    import_id: null,
    index_job_id: null,
    attempt: 1,
    error: null,
    created_at: '2026-11-19T11:00:00+00:00',
    updated_at: '2026-11-19T11:01:00+00:00',
    started_at: '2026-11-19T11:00:05+00:00',
    finished_at: null,
    ...overrides,
  };
}

function event(sequence: number, type: string): AutomationEvent {
  return {
    run_id: 'run_1',
    sequence,
    type,
    payload: {},
    created_at: '2026-11-19T11:00:05+00:00',
  };
}

function renderPage(path = '/schedule-runs/run_1') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/schedule-runs/:id" element={<ScheduleRunPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('ScheduleRunPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('uses the JSON polling fallback when EventSource is undefined', async () => {
    vi.stubGlobal('EventSource', undefined);
    mockGetRun.mockResolvedValue(run());
    mockGetRunEvents.mockImplementation(async (_id: string, after: number) =>
      [event(1, 'OCCURRENCE_DUE'), event(2, 'RUN_CLAIMED')].filter((item) => item.sequence > after),
    );

    renderPage();

    expect(await screen.findByText('Execução iniciada')).toBeInTheDocument();
    expect(screen.getByText('Ocorrência prevista')).toBeInTheDocument();
    expect(screen.getByText(/atualizando a cada 2 s/i)).toBeInTheDocument();
    expect(mockGetRunEvents).toHaveBeenCalledWith('run_1', 0);
    expect(screen.getByText('Em execução')).toBeInTheDocument();
  });

  it('labels every run state and hides the cancel action once terminal', async () => {
    vi.stubGlobal('EventSource', undefined);
    mockGetRunEvents.mockResolvedValue([]);
    mockGetRun.mockResolvedValue(run({ state: 'partial', phase: 'finalize', error: null }));

    renderPage();

    expect(await screen.findByText('Parcial')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Cancelar/i })).not.toBeInTheDocument();
  });

  it('offers cancel while the run is active', async () => {
    vi.stubGlobal('EventSource', undefined);
    mockGetRunEvents.mockResolvedValue([]);
    mockGetRun.mockResolvedValue(run({ state: 'waiting_children' }));

    renderPage();

    expect(await screen.findByText('Aguardando conversões')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Cancelar/i })).toBeInTheDocument();
  });

  it('shows the run error code and message for a failed occurrence', async () => {
    vi.stubGlobal('EventSource', undefined);
    mockGetRunEvents.mockResolvedValue([]);
    mockGetRun.mockResolvedValue(
      run({ state: 'failed', error: { code: 'SOURCE_NOT_FOUND', message: 'Fonte ausente.' } }),
    );

    renderPage();

    expect(await screen.findByText('Falhou')).toBeInTheDocument();
    expect(screen.getByText(/SOURCE_NOT_FOUND: Fonte ausente\./)).toBeInTheDocument();
    await waitFor(() => expect(mockGetRunEvents).toHaveBeenCalled());
  });
});
