/** Typed wrappers for the schedule endpoints (`/api/v1/schedules`). */

import type {
  AutomationEvent,
  AutomationRunRecord,
  ScheduleCreateRequest,
  SchedulePreviewResponse,
  ScheduleRecord,
} from '@pdf2x/contracts';
import { apiFetch } from '@/lib/api';

export type { AutomationRunRecord, ScheduleRecord };

export async function listSchedules(): Promise<ScheduleRecord[]> {
  const payload = await apiFetch<{ items: ScheduleRecord[] }>('/schedules');
  return payload.items ?? [];
}

export async function getSchedule(scheduleId: string): Promise<ScheduleRecord> {
  return apiFetch<ScheduleRecord>(`/schedules/${encodeURIComponent(scheduleId)}`);
}

export async function createSchedule(input: ScheduleCreateRequest): Promise<ScheduleRecord> {
  return apiFetch<ScheduleRecord>('/schedules', { method: 'POST', json: input });
}

export async function previewSchedule(input: ScheduleCreateRequest): Promise<SchedulePreviewResponse> {
  return apiFetch<SchedulePreviewResponse>('/schedules/preview', { method: 'POST', json: input });
}

export async function updateSchedule(
  scheduleId: string,
  revision: number,
  changes: Partial<ScheduleCreateRequest>,
): Promise<ScheduleRecord> {
  // PUT: the full-mode gateway declares @Put for schedules and the Python
  // service accepts PUT too. Sources stay on PATCH (see services/sources.ts).
  return apiFetch<ScheduleRecord>(`/schedules/${encodeURIComponent(scheduleId)}`, {
    method: 'PUT',
    json: { ...changes, revision },
  });
}

export async function archiveSchedule(scheduleId: string): Promise<{ archived: boolean }> {
  return apiFetch<{ archived: boolean }>(`/schedules/${encodeURIComponent(scheduleId)}`, {
    method: 'DELETE',
  });
}

export async function pauseSchedule(scheduleId: string): Promise<ScheduleRecord> {
  return apiFetch<ScheduleRecord>(`/schedules/${encodeURIComponent(scheduleId)}/pause`, {
    method: 'POST',
  });
}

export async function resumeSchedule(scheduleId: string): Promise<ScheduleRecord> {
  return apiFetch<ScheduleRecord>(`/schedules/${encodeURIComponent(scheduleId)}/resume`, {
    method: 'POST',
  });
}

export async function runNow(
  scheduleId: string,
  idempotencyKey: string,
): Promise<{ run_id: string; state: string; executor_active: boolean }> {
  return apiFetch<{ run_id: string; state: string; executor_active: boolean }>(
    `/schedules/${encodeURIComponent(scheduleId)}/run-now`,
    { method: 'POST', headers: { 'Idempotency-Key': idempotencyKey } },
  );
}

export async function listRuns(scheduleId: string): Promise<AutomationRunRecord[]> {
  const payload = await apiFetch<{ items: AutomationRunRecord[] }>(
    `/schedules/${encodeURIComponent(scheduleId)}/runs`,
  );
  return payload.items ?? [];
}

export async function getRun(runId: string): Promise<AutomationRunRecord> {
  return apiFetch<AutomationRunRecord>(`/schedule-runs/${encodeURIComponent(runId)}`);
}

export async function getRunEvents(runId: string, after = 0): Promise<AutomationEvent[]> {
  // Same route serves SSE for `Accept: text/event-stream`; the SPA asks for
  // the JSON page (fetch defaults to */*, so it gets JSON).
  const payload = await apiFetch<{ items: AutomationEvent[] }>(
    `/schedule-runs/${encodeURIComponent(runId)}/events`,
    { query: { after }, headers: { Accept: 'application/json' } },
  );
  return payload.items ?? [];
}

export async function cancelRun(runId: string): Promise<{ cancel_requested: boolean }> {
  return apiFetch<{ cancel_requested: boolean }>(
    `/schedule-runs/${encodeURIComponent(runId)}/cancel`,
    { method: 'POST' },
  );
}
