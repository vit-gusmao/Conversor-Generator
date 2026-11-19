import { useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import type { AutomationEvent, AutomationRunRecord } from '@pdf2x/contracts';
import { scheduleKeys } from '@/hooks/use-schedules';
import { buildUrl } from '@/lib/api';
import { explainError } from '@/lib/errors';
import { RUN_EVENT_LABELS, isRunTerminal } from '@/lib/schedule-labels';
import { getRunEvents } from '@/services/schedules';

/**
 * Timeline transport for one schedule occurrence.
 *
 * - `sse`: EventSource on `/schedule-runs/:id/events` (server pushes frames).
 * - `reconnecting`: the stream dropped; a new EventSource opens with `after=<last seq>`.
 * - `polling`: EventSource is unavailable or failed repeatedly; JSON pages are pulled
 *   every `pollIntervalMs` until the run row is terminal.
 * - `closed`: the server sent `done` (or polling saw a terminal run); nothing is open.
 */
export type RunStreamMode = 'idle' | 'connecting' | 'reconnecting' | 'sse' | 'polling' | 'closed';

export interface UseScheduleRunEventsOptions {
  /** Poll interval when the stream is unavailable. Default 2 s. */
  pollIntervalMs?: number;
  /** Reconnect backoff base: base * 2^(failures-1). Default 1 s. */
  baseDelayMs?: number;
  /** Consecutive stream failures before falling back to polling. Default 3. */
  maxStreamFailures?: number;
}

export interface UseScheduleRunEventsResult {
  /** De-duplicated by sequence, ascending. */
  events: AutomationEvent[];
  lastSequence: number;
  mode: RunStreamMode;
  error: string | null;
}

/** Server default page size for the JSON events endpoint. */
const PAGE_SIZE = 200;
const MAX_CATCH_UP_PAGES = 50;

/**
 * Named SSE frames: the server sends `event: <TYPE>`, and EventSource only
 * routes named frames to listeners registered for that exact name.
 */
export const KNOWN_RUN_EVENT_TYPES: readonly string[] = Object.keys(RUN_EVENT_LABELS);

export function scheduleRunEventsUrl(runId: string, after: number): string {
  return buildUrl(`/schedule-runs/${encodeURIComponent(runId)}/events`, {
    after: after > 0 ? after : undefined,
  });
}

/** Merges a batch into the timeline; existing sequences win, output is sorted. */
export function mergeRunEvents(current: AutomationEvent[], incoming: AutomationEvent[]): AutomationEvent[] {
  if (incoming.length === 0) return current;
  const bySequence = new Map<number, AutomationEvent>();
  for (const event of current) bySequence.set(event.sequence, event);
  let changed = false;
  for (const event of incoming) {
    if (!bySequence.has(event.sequence)) {
      bySequence.set(event.sequence, event);
      changed = true;
    }
  }
  if (!changed) return current;
  return [...bySequence.values()].sort((a, b) => a.sequence - b.sequence);
}

export function parseRunEvent(raw: unknown): AutomationEvent | null {
  if (typeof raw !== 'string') return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (typeof value !== 'object' || value === null) return null;
    const candidate = value as Partial<AutomationEvent>;
    if (typeof candidate.sequence !== 'number' || !Number.isFinite(candidate.sequence)) return null;
    if (typeof candidate.type !== 'string') return null;
    return {
      run_id: candidate.run_id ?? '',
      sequence: candidate.sequence,
      type: candidate.type,
      payload: candidate.payload ?? {},
      created_at: candidate.created_at ?? '',
    };
  } catch {
    return null;
  }
}

export function useScheduleRunEvents(
  runId: string | null,
  options: UseScheduleRunEventsOptions = {},
): UseScheduleRunEventsResult {
  const { pollIntervalMs = 2000, baseDelayMs = 1000, maxStreamFailures = 3 } = options;
  const queryClient = useQueryClient();
  const [events, setEvents] = useState<AutomationEvent[]>([]);
  const [mode, setMode] = useState<RunStreamMode>('idle');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setEvents([]);
    setError(null);
    if (!runId) {
      setMode('idle');
      return;
    }
    const id = runId;

    let cancelled = false;
    let finished = false;
    let failures = 0;
    let lastSequence = 0;
    let source: EventSource | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const clearTimer = () => {
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
    };

    const closeSource = () => {
      if (source) {
        source.close();
        source = null;
      }
    };

    const refreshRun = () => {
      void queryClient.invalidateQueries({ queryKey: scheduleKeys.run(id) });
    };

    const accept = (batch: AutomationEvent[]) => {
      if (cancelled || batch.length === 0) return;
      for (const event of batch) {
        if (event.sequence > lastSequence) lastSequence = event.sequence;
      }
      setEvents((previous) => mergeRunEvents(previous, batch));
      refreshRun();
    };

    /**
     * Pages the JSON endpoint from the last sequence held. It returns every
     * event type, so it also backfills types EventSource did not deliver.
     */
    const catchUp = async (): Promise<void> => {
      for (let page = 0; page < MAX_CATCH_UP_PAGES && !cancelled; page += 1) {
        const before = lastSequence;
        const batch = await getRunEvents(id, lastSequence);
        if (cancelled) return;
        accept(batch);
        if (batch.length < PAGE_SIZE || lastSequence === before) return;
      }
    };

    const safeCatchUp = async (): Promise<void> => {
      try {
        await catchUp();
        if (!cancelled) setError(null);
      } catch (cause) {
        if (!cancelled) setError(explainError(cause));
      }
    };

    const onStreamData = (raw: unknown) => {
      if (cancelled || finished) return;
      const parsed = parseRunEvent(raw);
      if (!parsed) return;
      if (parsed.sequence > lastSequence + 1) {
        // A gap means a frame was not routed to a listener: backfill, then accept.
        void safeCatchUp().then(() => accept([parsed]));
        return;
      }
      accept([parsed]);
    };

    const onNamedFrame = (message: MessageEvent) => onStreamData(message.data);

    const onDone = () => {
      if (cancelled || finished) return;
      finished = true;
      clearTimer();
      closeSource();
      void safeCatchUp().then(() => {
        if (cancelled) return;
        setMode('closed');
        refreshRun();
      });
    };

    const startPolling = () => {
      if (cancelled || finished) return;
      clearTimer();
      closeSource();
      setMode('polling');
      const tick = async () => {
        timer = null;
        if (cancelled || finished) return;
        const terminalBefore = isRunTerminal(
          queryClient.getQueryData<AutomationRunRecord>(scheduleKeys.run(id))?.state,
        );
        await safeCatchUp();
        if (cancelled) return;
        if (terminalBefore) {
          finished = true;
          setMode('closed');
          return;
        }
        timer = setTimeout(() => void tick(), pollIntervalMs);
      };
      void tick();
    };

    const scheduleReconnect = () => {
      if (cancelled || finished) return;
      clearTimer();
      setMode('reconnecting');
      const delay = baseDelayMs * 2 ** Math.max(0, failures - 1);
      timer = setTimeout(() => {
        timer = null;
        connect();
      }, delay);
    };

    const onStreamFailure = () => {
      closeSource();
      if (cancelled || finished) return;
      failures += 1;
      if (failures >= maxStreamFailures) startPolling();
      else scheduleReconnect();
    };

    const connect = () => {
      if (cancelled || finished) return;
      if (typeof EventSource === 'undefined') {
        startPolling();
        return;
      }
      setMode(failures > 0 ? 'reconnecting' : 'connecting');
      let stream: EventSource;
      try {
        // Same-origin requests carry the device cookie; withCredentials keeps
        // it on cross-origin dev/proxy setups too.
        stream = new EventSource(scheduleRunEventsUrl(id, lastSequence), { withCredentials: true });
      } catch {
        onStreamFailure();
        return;
      }
      source = stream;
      stream.onopen = () => {
        if (cancelled || finished || source !== stream) return;
        failures = 0;
        setError(null);
        setMode('sse');
      };
      stream.onmessage = (message) => onStreamData(message.data);
      for (const type of KNOWN_RUN_EVENT_TYPES) stream.addEventListener(type, onNamedFrame);
      stream.addEventListener('done', onDone);
      stream.onerror = () => {
        if (source !== stream) return;
        onStreamFailure();
      };
    };

    connect();

    return () => {
      cancelled = true;
      clearTimer();
      closeSource();
    };
  }, [runId, queryClient, pollIntervalMs, baseDelayMs, maxStreamFailures]);

  const lastSequence = events.length > 0 ? events[events.length - 1]!.sequence : 0;
  return { events, lastSequence, mode, error };
}
