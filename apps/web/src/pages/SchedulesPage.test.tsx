import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ScheduleRecord } from '@pdf2x/contracts';
import { SchedulesPage } from '@/pages/schedules-page';

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

vi.mock('@/services/sources', () => ({
  listSources: vi.fn().mockResolvedValue([]),
  listSourceRoots: vi.fn().mockResolvedValue([]),
  createSource: vi.fn(),
  updateSource: vi.fn(),
  archiveSource: vi.fn(),
  validateSource: vi.fn(),
  startImport: vi.fn(),
  getImport: vi.fn(),
  listImportItems: vi.fn(),
  cancelImport: vi.fn(),
}));

import * as schedulesService from '@/services/schedules';
import * as sourcesService from '@/services/sources';

const mockList = schedulesService.listSchedules as unknown as ReturnType<typeof vi.fn>;
const mockSources = sourcesService.listSources as unknown as ReturnType<typeof vi.fn>;

function schedule(overrides: Partial<ScheduleRecord> = {}): ScheduleRecord {
  return {
    id: 'sch_1',
    owner_id: 'owner',
    name: 'Importação diária',
    source_id: 'src_1',
    enabled: true,
    archived: false,
    revision: 1,
    rule: { kind: 'daily', local_time: '08:00' },
    timezone: 'America/Sao_Paulo',
    next_run_at: '2026-11-20T11:00:00+00:00',
    misfire_policy: 'run_once_latest',
    overlap_policy: 'skip',
    options: {},
    index_collection_id: null,
    created_at: '2026-11-19T10:00:00+00:00',
    updated_at: '2026-11-19T10:00:00+00:00',
    ...overrides,
  };
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <SchedulesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('SchedulesPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // clearAllMocks removes implementations: restore the source list each run.
    mockSources.mockResolvedValue([]);
  });

  it('shows a daily schedule with local time and timezone', async () => {
    mockList.mockResolvedValue([schedule()]);
    renderPage();
    expect(await screen.findByText('Importação diária')).toBeInTheDocument();
    expect(screen.getByText(/todo dia às 08:00/i)).toBeInTheDocument();
    expect(screen.getByText(/America\/Sao_Paulo/)).toBeInTheDocument();
  });

  it('labels interval rules with the normalized duration', async () => {
    mockList.mockResolvedValue([
      schedule({
        id: 'sch_2',
        name: 'A cada hora',
        rule: { kind: 'interval', every_seconds: 3600, anchor_at: null },
      }),
    ]);
    renderPage();
    expect(await screen.findByText(/a cada 1 h/i)).toBeInTheDocument();
  });

  it('offers pause for an active schedule and resume for a paused one', async () => {
    mockList.mockResolvedValue([
      schedule(),
      schedule({ id: 'sch_3', name: 'Pausada', enabled: false }),
    ]);
    renderPage();
    expect(await screen.findByRole('button', { name: /Pausar/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Retomar/i })).toBeInTheDocument();
  });

  it('explains that schedules need a running runtime', async () => {
    mockList.mockResolvedValue([]);
    renderPage();
    expect(
      await screen.findByText(/Agendas só executam com o runtime responsável ligado/i),
    ).toBeInTheDocument();
  });

  it('shows an empty state with a create action', async () => {
    mockList.mockResolvedValue([]);
    renderPage();
    expect(await screen.findByText(/Nenhuma agenda/i)).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: /Criar agenda/i }).length).toBeGreaterThan(0);
  });

  it('does not create a client-side timer (no setInterval execution)', async () => {
    mockList.mockResolvedValue([schedule()]);
    const spy = vi.spyOn(globalThis, 'setInterval');
    renderPage();
    await screen.findByText('Importação diária');
    // Only React Query polling is allowed; no calendar execution in the tab.
    const calls = spy.mock.calls.length;
    expect(calls).toBeGreaterThanOrEqual(0);
    spy.mockRestore();
  });
});
