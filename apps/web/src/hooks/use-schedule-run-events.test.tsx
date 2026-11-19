import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import type { AutomationEvent } from '@pdf2x/contracts';
import {
  KNOWN_RUN_EVENT_TYPES,
  mergeRunEvents,
  parseRunEvent,
  scheduleRunEventsUrl,
  useScheduleRunEvents,
} from '@/hooks/use-schedule-run-events';

vi.mock('@/services/schedules', () => ({
  getRunEvents: vi.fn(),
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
  cancelRun: vi.fn(),
}));

import * as schedulesService from '@/services/schedules';

const mockGetRunEvents = schedulesService.getRunEvents as unknown as ReturnType<typeof vi.fn>;

function event(sequence: number, type = 'RUN_CLAIMED'): AutomationEvent {
  return {
    run_id: 'run_1',
    sequence,
    type,
    payload: { sequence },
    created_at: '2026-11-19T11:00:00+00:00',
  };
}

/** Minimal EventSource double: records listeners so tests can emit frames. */
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  readonly url: string;
  readonly withCredentials: boolean;
  readyState = 0;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  readonly close = vi.fn(() => {
    this.readyState = 2;
  });
  private readonly listeners = new Map<string, Set<(ev: MessageEvent) => void>>();

  constructor(url: string, init?: { withCredentials?: boolean }) {
    this.url = url;
    this.withCredentials = Boolean(init?.withCredentials);
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (ev: MessageEvent) => void) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type)!.add(listener);
  }

  open() {
    this.readyState = 1;
    this.onopen?.(new Event('open'));
  }

  emit(type: string, data: unknown) {
    const message = { data: typeof data === 'string' ? data : JSON.stringify(data) } as MessageEvent;
    if (type === 'message') {
      this.onmessage?.(message);
      return;
    }
    this.listeners.get(type)?.forEach((listener) => listener(message));
  }

  fail() {
    this.onerror?.(new Event('error'));
  }
}

function wrapperFor(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

function newClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
}

beforeEach(() => {
  FakeEventSource.instances = [];
  mockGetRunEvents.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('run event helpers', () => {
  it('merges batches by sequence without duplicates and keeps order', () => {
    const merged = mergeRunEvents([event(2), event(1)], [event(2), event(3)]);
    expect(merged.map((item) => item.sequence)).toEqual([1, 2, 3]);
  });

  it('returns the same array when nothing new arrives', () => {
    const current = [event(1)];
    expect(mergeRunEvents(current, [event(1)])).toBe(current);
  });

  it('rejects frames without a numeric sequence or a type', () => {
    expect(parseRunEvent('{"type":"RUN_CLAIMED"}')).toBeNull();
    expect(parseRunEvent('{"sequence":1}')).toBeNull();
    expect(parseRunEvent('not json')).toBeNull();
    expect(parseRunEvent(JSON.stringify(event(4)))?.sequence).toBe(4);
  });

  it('builds the SSE URL with the resume cursor only when there is one', () => {
    expect(scheduleRunEventsUrl('run_1', 0)).toBe('/api/v1/schedule-runs/run_1/events');
    expect(scheduleRunEventsUrl('run_1', 7)).toBe('/api/v1/schedule-runs/run_1/events?after=7');
  });

  it('registers listeners for every named frame the backend emits', () => {
    expect(KNOWN_RUN_EVENT_TYPES).toEqual(expect.arrayContaining(['RUN_CLAIMED', 'RUN_FINISHED']));
  });
});

describe('useScheduleRunEvents', () => {
  it('uses polling when EventSource is undefined and stops after unmount', async () => {
    vi.stubGlobal('EventSource', undefined);
    const fixture = [event(1, 'OCCURRENCE_DUE'), event(2, 'RUN_CLAIMED')];
    mockGetRunEvents.mockImplementation(async (_runId: string, after: number) =>
      fixture.filter((item) => item.sequence > after),
    );

    const { result, unmount } = renderHook(
      () => useScheduleRunEvents('run_1', { pollIntervalMs: 10 }),
      { wrapper: wrapperFor(newClient()) },
    );

    await waitFor(() => expect(result.current.events).toHaveLength(2));
    expect(result.current.mode).toBe('polling');
    expect(mockGetRunEvents).toHaveBeenCalledWith('run_1', 0);
    expect(mockGetRunEvents).toHaveBeenLastCalledWith('run_1', 2);

    unmount();
    const callsAtUnmount = mockGetRunEvents.mock.calls.length;
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(mockGetRunEvents.mock.calls.length).toBe(callsAtUnmount);
  });

  it('opens one EventSource with credentials, de-duplicates frames and closes on unmount', async () => {
    vi.stubGlobal('EventSource', FakeEventSource);
    const { result, unmount } = renderHook(() => useScheduleRunEvents('run_1'), {
      wrapper: wrapperFor(newClient()),
    });

    expect(FakeEventSource.instances).toHaveLength(1);
    const stream = FakeEventSource.instances[0]!;
    expect(stream.url).toBe('/api/v1/schedule-runs/run_1/events');
    expect(stream.withCredentials).toBe(true);
    expect(result.current.mode).toBe('connecting');

    act(() => stream.open());
    expect(result.current.mode).toBe('sse');

    act(() => {
      stream.emit('RUN_CLAIMED', event(1));
      stream.emit('RUN_CLAIMED', event(1));
      stream.emit('message', event(2, 'UNKNOWN_TYPE_ON_MESSAGE'));
    });

    expect(result.current.events.map((item) => item.sequence)).toEqual([1, 2]);
    expect(result.current.lastSequence).toBe(2);

    unmount();
    expect(stream.close).toHaveBeenCalled();
  });

  it('backfills from the JSON endpoint when a sequence gap appears', async () => {
    vi.stubGlobal('EventSource', FakeEventSource);
    mockGetRunEvents.mockResolvedValueOnce([event(2), event(3)]).mockResolvedValue([]);

    const { result, unmount } = renderHook(() => useScheduleRunEvents('run_1'), {
      wrapper: wrapperFor(newClient()),
    });
    const stream = FakeEventSource.instances[0]!;
    act(() => {
      stream.open();
      stream.emit('RUN_CLAIMED', event(1));
    });
    act(() => stream.emit('RUN_FINISHED', event(4, 'RUN_FINISHED')));

    await waitFor(() =>
      expect(result.current.events.map((item) => item.sequence)).toEqual([1, 2, 3, 4]),
    );
    expect(mockGetRunEvents).toHaveBeenCalledWith('run_1', 1);
    unmount();
  });

  it('reconnects with after=<last sequence> and falls back to polling after repeated failures', async () => {
    vi.stubGlobal('EventSource', FakeEventSource);
    mockGetRunEvents.mockResolvedValue([]);

    const { result, unmount } = renderHook(
      () => useScheduleRunEvents('run_1', { baseDelayMs: 1, pollIntervalMs: 10 }),
      { wrapper: wrapperFor(newClient()) },
    );

    act(() => {
      FakeEventSource.instances[0]!.open();
      FakeEventSource.instances[0]!.emit('RUN_CLAIMED', event(2));
    });

    act(() => FakeEventSource.instances[0]!.fail());
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(2));
    expect(FakeEventSource.instances[1]!.url).toBe('/api/v1/schedule-runs/run_1/events?after=2');
    expect(FakeEventSource.instances[0]!.close).toHaveBeenCalled();

    act(() => FakeEventSource.instances[1]!.fail());
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(3));

    act(() => FakeEventSource.instances[2]!.fail());
    await waitFor(() => expect(result.current.mode).toBe('polling'));
    await waitFor(() => expect(mockGetRunEvents).toHaveBeenCalledWith('run_1', 2));
    expect(FakeEventSource.instances[2]!.close).toHaveBeenCalled();
    unmount();
  });

  it('closes the stream and stops when the server sends done', async () => {
    vi.stubGlobal('EventSource', FakeEventSource);
    mockGetRunEvents.mockResolvedValue([]);
    const { result, unmount } = renderHook(() => useScheduleRunEvents('run_1'), {
      wrapper: wrapperFor(newClient()),
    });
    const stream = FakeEventSource.instances[0]!;
    act(() => stream.open());
    act(() => stream.emit('done', {}));

    await waitFor(() => expect(result.current.mode).toBe('closed'));
    expect(stream.close).toHaveBeenCalled();
    expect(FakeEventSource.instances).toHaveLength(1);
    unmount();
  });
});
