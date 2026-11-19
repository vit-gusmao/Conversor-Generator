import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { STALE_TIME } from '@/lib/query-keys';
import { isRunTerminal, toScheduleCreateRequest } from '@/lib/schedule-labels';
import {
  archiveSchedule,
  cancelRun,
  createSchedule,
  getRun,
  listRuns,
  getSchedule,
  listSchedules,
  pauseSchedule,
  previewSchedule,
  resumeSchedule,
  runNow,
  updateSchedule,
} from '@/services/schedules';
import type { AutomationRunRecord, ScheduleRecord } from '@pdf2x/contracts';

export const scheduleKeys = {
  list: ['schedules', 'list'] as const,
  detail: (id: string) => ['schedules', 'detail', id] as const,
  preview: (input: unknown) => ['schedules', 'preview', input] as const,
  occurrences: (id: string, revision: number) => ['schedules', 'occurrences', id, revision] as const,
  runs: (id: string) => ['schedules', id, 'runs'] as const,
  run: (id: string) => ['schedule-runs', id] as const,
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

/** Next occurrences computed by the same rule the scheduler uses (preview endpoint). */
export function useScheduleOccurrences(schedule: ScheduleRecord | null | undefined) {
  return useQuery({
    queryKey: scheduleKeys.occurrences(schedule?.id ?? 'none', schedule?.revision ?? 0),
    queryFn: () => previewSchedule(toScheduleCreateRequest(schedule as ScheduleRecord)),
    enabled: Boolean(schedule) && !schedule?.archived,
    staleTime: STALE_TIME.preview,
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
    // The revision lives in the detail query: refresh it too, or the next save
    // would send a stale revision and hit REVISION_CONFLICT.
    onSuccess: (_data, input) => {
      void queryClient.invalidateQueries({ queryKey: scheduleKeys.detail(input.scheduleId) });
      void queryClient.invalidateQueries({ queryKey: scheduleKeys.list });
      void queryClient.invalidateQueries({ queryKey: ['schedules'] });
    },
  });
}

export function useScheduleActions() {
  const queryClient = useQueryClient();
  const invalidate = () => {
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
    // Only while an occurrence is still moving; a settled history does not poll.
    refetchInterval: (query) => {
      const rows = (query.state.data ?? []) as AutomationRunRecord[];
      return rows.some((run) => !isRunTerminal(run.state)) ? 5_000 : false;
    },
  });
}

/**
 * The run row is the authority for state. Pass `poll: false` while the SSE
 * event stream is healthy; the stream invalidates this query on each batch.
 */
export function useRun(runId: string | null, options: { poll?: boolean } = {}) {
  const poll = options.poll ?? true;
  return useQuery({
    queryKey: scheduleKeys.run(runId ?? 'none'),
    queryFn: () => getRun(runId as string),
    enabled: Boolean(runId),
    refetchInterval: (query) => {
      if (!poll) return false;
      const data = query.state.data;
      if (!data) return 2000;
      return isRunTerminal(data.state) ? false : 2000;
    },
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
