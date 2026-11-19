import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { AutomationRunRecord, RunState, ScheduleRecord, SourceRecord } from '@pdf2x/contracts';
import { ScheduleDetailPage } from '@/pages/schedule-detail-page';
import { ApiError } from '@/lib/api';

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
  listSources: vi.fn(),
  listSourceRoots: vi.fn(),
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

const mockGetSchedule = schedulesService.getSchedule as unknown as ReturnType<typeof vi.fn>;
const mockPreview = schedulesService.previewSchedule as unknown as ReturnType<typeof vi.fn>;
const mockListRuns = schedulesService.listRuns as unknown as ReturnType<typeof vi.fn>;
const mockPause = schedulesService.pauseSchedule as unknown as ReturnType<typeof vi.fn>;
const mockRunNow = schedulesService.runNow as unknown as ReturnType<typeof vi.fn>;
const mockSources = sourcesService.listSources as unknown as ReturnType<typeof vi.fn>;

function schedule(overrides: Partial<ScheduleRecord> = {}): ScheduleRecord {
  return {
    id: 'sch_1',
    owner_id: 'owner',
    name: 'Importação diária',
    source_id: 'src_1',
    enabled: true,
    archived: false,
    revision: 3,
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

function source(overrides: Partial<SourceRecord> = {}): SourceRecord {
  return {
    id: 'src_1',
    owner_id: 'owner',
    root_id: 'root_1',
    relative_path: 'financeiro',
    kind: 'directory',
    name: 'Financeiro',
    recursive: false,
    include_patterns: null,
    exclude_patterns: null,
    enabled: true,
    version: 1,
    created_at: '2026-11-19T10:00:00+00:00',
    updated_at: '2026-11-19T10:00:00+00:00',
    ...overrides,
  };
}

function run(id: string, state: RunState, scheduledFor: string): AutomationRunRecord {
  return {
    id,
    owner_id: 'owner',
    schedule_id: 'sch_1',
    schedule_revision: 3,
    scheduled_for: scheduledFor,
    trigger: 'schedule',
    occurrence_key: `${id}-key`,
    state,
    phase: null,
    import_id: null,
    index_job_id: null,
    attempt: 1,
    error: null,
    created_at: scheduledFor,
    updated_at: scheduledFor,
    started_at: null,
    finished_at: null,
  };
}

const ALL_STATES: RunState[] = [
  'queued',
  'running',
  'waiting_children',
  'cancelling',
  'succeeded',
  'partial',
  'no_changes',
  'failed',
  'cancelled',
  'skipped',
];

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/schedules/sch_1']}>
        <Routes>
          <Route path="/schedules/:id" element={<ScheduleDetailPage />} />
          <Route path="/schedules/:id/edit" element={<p>Editor</p>} />
          <Route path="/schedules" element={<p>Lista de agendas</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('ScheduleDetailPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockSources.mockResolvedValue([source()]);
    mockListRuns.mockResolvedValue([]);
    mockPreview.mockResolvedValue({
      next_occurrences: [
        '2026-11-20T11:00:00+00:00',
        '2026-11-21T11:00:00+00:00',
        '2026-11-22T11:00:00+00:00',
        '2026-11-23T11:00:00+00:00',
        '2026-11-24T11:00:00+00:00',
      ],
      timezone: 'America/Sao_Paulo',
      rule: { kind: 'daily', local_time: '08:00' },
    });
  });

  it('summarises the rule in Portuguese with timezone, source and state', async () => {
    mockGetSchedule.mockResolvedValue(schedule());
    renderPage();

    expect(await screen.findByRole('heading', { name: 'Importação diária' })).toBeInTheDocument();
    expect(screen.getByText('todo dia às 08:00')).toBeInTheDocument();
    expect(screen.getByText('America/Sao_Paulo')).toBeInTheDocument();
    expect(screen.getByText('Financeiro')).toBeInTheDocument();
    expect(screen.getByText('ativa')).toBeInTheDocument();
    expect(screen.getByText('revisão 3')).toBeInTheDocument();
  });

  it('previews the next five occurrences using the schedule rule and timezone', async () => {
    mockGetSchedule.mockResolvedValue(schedule());
    renderPage();

    await waitFor(() => expect(mockPreview).toHaveBeenCalled());
    expect(mockPreview.mock.calls[0]![0]).toMatchObject({
      rule: { kind: 'daily', local_time: '08:00' },
      timezone: 'America/Sao_Paulo',
      source_id: 'src_1',
    });
    const items = await screen.findAllByText(/\(America\/Sao_Paulo\)/, { selector: 'span' });
    expect(items).toHaveLength(5);
  });

  it('lists every run state with its Portuguese label and links to the run', async () => {
    mockGetSchedule.mockResolvedValue(schedule());
    mockListRuns.mockResolvedValue(
      ALL_STATES.map((state, index) =>
        run(`run_${index}`, state, `2026-11-1${Math.min(index, 9)}T11:00:00+00:00`),
      ),
    );
    renderPage();

    const expected = [
      'Na fila',
      'Em execução',
      'Aguardando conversões',
      'Cancelando',
      'Concluída',
      'Parcial',
      'Sem alterações',
      'Falhou',
      'Cancelada',
      'Ignorada',
    ];
    for (const label of expected) {
      expect(await screen.findByText(label)).toBeInTheDocument();
    }
    const link = screen.getAllByRole('link').find((item) => item.getAttribute('href') === '/schedule-runs/run_0');
    expect(link).toBeDefined();
  });

  it('warns when the schedule source is missing or disabled', async () => {
    mockGetSchedule.mockResolvedValue(schedule());
    mockSources.mockResolvedValue([source({ enabled: false })]);
    renderPage();

    expect(await screen.findByText('Fonte indisponível')).toBeInTheDocument();
  });

  it('does not warn while the source list is still loading or the source is available', async () => {
    mockGetSchedule.mockResolvedValue(schedule());
    renderPage();

    await screen.findByRole('heading', { name: 'Importação diária' });
    expect(screen.queryByText('Fonte indisponível')).not.toBeInTheDocument();
  });

  it('pauses an active schedule through the service', async () => {
    mockGetSchedule.mockResolvedValue(schedule());
    mockPause.mockResolvedValue(schedule({ enabled: false }));
    renderPage();

    await userEvent.click(await screen.findByRole('button', { name: /Pausar/i }));
    await waitFor(() => expect(mockPause).toHaveBeenCalled());
    expect(mockPause.mock.calls[0]![0]).toBe('sch_1');
  });

  it('runs now with an idempotency key and reports the outcome', async () => {
    mockGetSchedule.mockResolvedValue(schedule());
    mockRunNow.mockResolvedValue({ run_id: 'run_9', state: 'queued' });
    renderPage();

    await userEvent.click(await screen.findByRole('button', { name: /Executar agora/i }));
    await waitFor(() => expect(mockRunNow).toHaveBeenCalled());
    expect(mockRunNow.mock.calls[0]![0]).toBe('sch_1');
    expect(String(mockRunNow.mock.calls[0]![1])).toMatch(/^sch_1:\d+$/);
  });

  it('disables run-now while a request is pending so one click cannot double-submit', async () => {
    mockGetSchedule.mockResolvedValue(schedule());
    mockRunNow.mockReturnValue(new Promise(() => undefined));
    renderPage();

    const button = await screen.findByRole('button', { name: /Executar agora/i });
    await userEvent.click(button);
    await waitFor(() => expect(screen.getByRole('button', { name: /Executar agora/i })).toBeDisabled());
    await userEvent.click(screen.getByRole('button', { name: /Executar agora/i }));
    expect(mockRunNow).toHaveBeenCalledTimes(1);
  });

  it('hides edit and run actions for an archived schedule', async () => {
    mockGetSchedule.mockResolvedValue(schedule({ archived: true, enabled: false }));
    renderPage();

    expect(await screen.findByText('arquivada')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Executar agora/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /Editar/i })).not.toBeInTheDocument();
  });

  it('shows the API error when the schedule does not exist', async () => {
    mockGetSchedule.mockRejectedValue(
      new ApiError({ code: 'SCHEDULE_NOT_FOUND', message: 'nope', status: 404 }),
    );
    renderPage();

    expect(await screen.findByText(/Esta agenda não existe mais/)).toBeInTheDocument();
  });
});
