import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { STALE_TIME } from '@/lib/query-keys';
import {
  archiveSchedule,
  cancelRun,
  createSchedule,
  getRun,
  getRunEvents,
  getSchedule,
  listRuns,
  listSchedules,
  pauseSchedule,
  previewSchedule,
  resumeSchedule,
  runNow,
  updateSchedule,
} from '@/services/schedules';

export const scheduleKeys = {
  list: ['schedules', 'list'] as const,
  detail: (id: string) => ['schedules', 'detail', id] as const,
  preview: (input: unknown) => ['schedules', 'preview', input] as const,
  runs: (id: string) => ['schedules', id, 'runs'] as const,
  run: (id: string) => ['schedule-runs', id] as const,
  runEvents: (id: string) => ['schedule-runs', id, 'events'] as const,
};

export function useSchedules() {
  return useQuery({
    queryKey: scheduleKeys.list,
    queryFn: listSchedules,
    staleTime: STALE_TIME.history,
  });
}

export function useSchedule(id: string | null) {
  return useQuery({
    queryKey: scheduleKeys.detail(id ?? 'none'),
    queryFn: () => getSchedule(id as string),
    enabled: Boolean(id),
    staleTime: STALE_TIME.settings,
  });
}

export function useCreateSchedule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createSchedule,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: scheduleKeys.list }),
  });
}

export function usePreviewSchedule() {
  return useMutation({ mutationFn: previewSchedule });
}

export function useUpdateSchedule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { scheduleId: string; revision: number; changes: Record<string, unknown> }) =>
      updateSchedule(input.scheduleId, input.revision, input.changes),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: scheduleKeys.list }),
  });
}

export function useScheduleActions() {
  const queryClient = useQueryClient();
  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: scheduleKeys.list });
    void queryClient.invalidateQueries({ queryKey: ['schedules'] });
  };
  return {
    pause: useMutation({ mutationFn: pauseSchedule, onSuccess: invalidate }),
    resume: useMutation({ mutationFn: resumeSchedule, onSuccess: invalidate }),
    archive: useMutation({ mutationFn: archiveSchedule, onSuccess: invalidate }),
    runNow: useMutation({
      mutationFn: (input: { scheduleId: string; idempotencyKey: string }) =>
        runNow(input.scheduleId, input.idempotencyKey),
      onSuccess: invalidate,
    }),
  };
}

export function useScheduleRuns(scheduleId: string | null) {
  return useQuery({
    queryKey: scheduleKeys.runs(scheduleId ?? 'none'),
    queryFn: () => listRuns(scheduleId as string),
    enabled: Boolean(scheduleId),
    staleTime: 3_000,
    refetchInterval: 5_000,
  });
}

export function useRun(runId: string | null) {
  return useQuery({
    queryKey: scheduleKeys.run(runId ?? 'none'),
    queryFn: () => getRun(runId as string),
    enabled: Boolean(runId),
    // Polling fallback for the SSE stream: the row is the authority.
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data) return 2000;
      return ['succeeded', 'partial', 'no_changes', 'failed', 'cancelled', 'skipped'].includes(
        data.state,
      )
        ? false
        : 2000;
    },
    staleTime: 0,
  });
}

export function useRunEvents(runId: string | null) {
  return useQuery({
    queryKey: scheduleKeys.runEvents(runId ?? 'none'),
    queryFn: () => getRunEvents(runId as string),
    enabled: Boolean(runId),
    refetchInterval: 3_000,
    staleTime: 0,
  });
}

export function useCancelRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (runId: string) => cancelRun(runId),
    onSuccess: (_data, runId) => {
      void queryClient.invalidateQueries({ queryKey: scheduleKeys.run(runId) });
    },
  });
}
