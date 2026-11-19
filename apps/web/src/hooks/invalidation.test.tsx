import { describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { useUpdateSchedule } from '@/hooks/use-schedules';
import { useUpdateSource } from '@/hooks/use-sources';

vi.mock('@/services/schedules', () => ({
  updateSchedule: vi.fn().mockResolvedValue({ id: 'sch_1' }),
  listSchedules: vi.fn(),
  getSchedule: vi.fn(),
  createSchedule: vi.fn(),
  previewSchedule: vi.fn(),
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
  updateSource: vi.fn().mockResolvedValue({ id: 'src_1' }),
  listSourceRoots: vi.fn(),
  listSources: vi.fn(),
  createSource: vi.fn(),
  archiveSource: vi.fn(),
  validateSource: vi.fn(),
  startImport: vi.fn(),
  getImport: vi.fn(),
  listImportItems: vi.fn(),
  cancelImport: vi.fn(),
}));

function setup() {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  const spy = vi.spyOn(client, 'invalidateQueries');
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, spy, wrapper };
}

describe('update mutations refresh the detail they came from', () => {
  it('invalidates the schedule detail key after a schedule update', async () => {
    const { spy, wrapper } = setup();
    const { result } = renderHook(() => useUpdateSchedule(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ scheduleId: 'sch_1', revision: 1, changes: {} });
    });

    const keys = spy.mock.calls.map((call) => JSON.stringify(call[0]?.queryKey));
    expect(keys).toContain(JSON.stringify(['schedules', 'detail', 'sch_1']));
    expect(keys).toContain(JSON.stringify(['schedules', 'list']));
  });

  it('invalidates the source detail key after a source update', async () => {
    const { spy, wrapper } = setup();
    const { result } = renderHook(() => useUpdateSource(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ sourceId: 'src_1', version: 1, changes: {} });
    });

    const keys = spy.mock.calls.map((call) => JSON.stringify(call[0]?.queryKey));
    expect(keys).toContain(JSON.stringify(['sources', 'detail', 'src_1']));
    expect(keys).toContain(JSON.stringify(['sources', 'list']));
  });
});
