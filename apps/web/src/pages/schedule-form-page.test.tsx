import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ScheduleRecord, SourceRecord } from '@pdf2x/contracts';
import { ScheduleFormPage } from '@/pages/schedule-form-page';
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
const mockCreate = schedulesService.createSchedule as unknown as ReturnType<typeof vi.fn>;
const mockUpdate = schedulesService.updateSchedule as unknown as ReturnType<typeof vi.fn>;
const mockSources = sourcesService.listSources as unknown as ReturnType<typeof vi.fn>;

function source(): SourceRecord {
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
  };
}

function schedule(overrides: Partial<ScheduleRecord> = {}): ScheduleRecord {
  return {
    id: 'sch_1',
    owner_id: 'owner',
    name: 'Importação diária',
    source_id: 'src_1',
    enabled: true,
    archived: false,
    revision: 2,
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

function renderAt(path: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/schedules/new" element={<ScheduleFormPage />} />
          <Route path="/schedules/:id/edit" element={<ScheduleFormPage />} />
          <Route path="/schedules/:id" element={<p>Detalhe da agenda</p>} />
          <Route path="/schedules" element={<p>Lista de agendas</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('ScheduleFormPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockSources.mockResolvedValue([source()]);
  });

  it('sends the explicit timezone and daily time, then opens the schedule detail', async () => {
    mockCreate.mockResolvedValue(schedule({ id: 'sch_new' }));
    renderAt('/schedules/new?source=src_1');

    await userEvent.type(screen.getByPlaceholderText('Importação diária'), 'Diária');
    fireEvent.change(screen.getByDisplayValue('08:00'), { target: { value: '09:30' } });
    await userEvent.click(screen.getByRole('button', { name: 'Criar agenda' }));

    await waitFor(() => expect(mockCreate).toHaveBeenCalled());
    expect(mockCreate.mock.calls[0]![0]).toMatchObject({
      name: 'Diária',
      source_id: 'src_1',
      timezone: 'America/Sao_Paulo',
      rule: { kind: 'daily', local_time: '09:30' },
    });
    expect(await screen.findByText('Detalhe da agenda')).toBeInTheDocument();
  });

  it('rejects an interval under 60 seconds before any request is sent', async () => {
    mockGetSchedule.mockResolvedValue(
      schedule({ rule: { kind: 'interval', every_seconds: 3600, anchor_at: null } }),
    );
    renderAt('/schedules/sch_1/edit');

    await screen.findByDisplayValue('Importação diária');
    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '0' } });

    expect(screen.getByText(/Duração: 0 s/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Salvar alterações' }));
    expect(mockUpdate).not.toHaveBeenCalled();
  });

  it('shows a clear conflict message on 409 and reloads the latest revision on request', async () => {
    mockGetSchedule
      .mockResolvedValueOnce(schedule({ revision: 2 }))
      .mockResolvedValue(schedule({ revision: 3, name: 'Versão nova' }));
    mockUpdate.mockRejectedValue(
      new ApiError({
        code: 'REVISION_CONFLICT',
        message: 'Revisão da agenda divergente: releia e reaplique.',
        status: 409,
      }),
    );
    renderAt('/schedules/sch_1/edit');

    const nameInput = await screen.findByDisplayValue('Importação diária');
    fireEvent.change(nameInput, { target: { value: 'Minha edição' } });
    await userEvent.click(screen.getByRole('button', { name: 'Salvar alterações' }));

    expect(await screen.findByText('Esta agenda foi alterada em outro lugar')).toBeInTheDocument();
    expect(mockUpdate.mock.calls[0]![1]).toBe(2);

    await userEvent.click(screen.getByRole('button', { name: /Recarregar versão mais recente/i }));

    expect(await screen.findByDisplayValue('Versão nova')).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByText('Esta agenda foi alterada em outro lugar')).not.toBeInTheDocument(),
    );
  });

  it('navigates to the detail page after a successful update', async () => {
    mockGetSchedule.mockResolvedValue(schedule());
    mockUpdate.mockResolvedValue(schedule({ revision: 3 }));
    renderAt('/schedules/sch_1/edit');

    await screen.findByDisplayValue('Importação diária');
    await userEvent.click(screen.getByRole('button', { name: 'Salvar alterações' }));

    expect(await screen.findByText('Detalhe da agenda')).toBeInTheDocument();
    expect(mockUpdate.mock.calls[0]![0]).toBe('sch_1');
  });
});
