import { useEffect, useRef, useState } from 'react';
import type { ConversionEvent } from '@pdf2x/contracts';
import { isTerminal } from '@pdf2x/contracts';
import { authHeaders, resolveToken } from '@/lib/api';
import { conversionEventsUrl } from '@/services/conversions';

/* ------------------------------------------------------------------------- */
/* SSE framing                                                                */
/* ------------------------------------------------------------------------- */

export interface SseFrame {
  id?: string;
  event?: string;
  data: string;
}

export function parseSseFrame(raw: string): SseFrame | null {
  let id: string | undefined;
  let event: string | undefined;
  const dataLines: string[] = [];

  for (const line of raw.split('\n')) {
    if (!line || line.startsWith(':')) continue;
    const separator = line.indexOf(':');
    const field = separator === -1 ? line : line.slice(0, separator);
    let value = separator === -1 ? '' : line.slice(separator + 1);
    if (value.startsWith(' ')) value = value.slice(1);

    if (field === 'id') id = value;
    else if (field === 'event') event = value;
    else if (field === 'data') dataLines.push(value);
  }

  if (dataLines.length === 0) return null;
  return { id, event, data: dataLines.join('\n') };
}

/** Incremental SSE parser: feed decoded chunks, receive complete frames. */
export function createSseParser(onFrame: (frame: SseFrame) => void): (chunk: string) => void {
  let buffer = '';
  return (chunk: string) => {
    buffer += chunk.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
    let boundary = buffer.indexOf('\n\n');
    while (boundary !== -1) {
      const raw = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const frame = parseSseFrame(raw);
      if (frame) onFrame(frame);
      boundary = buffer.indexOf('\n\n');
    }
  };
}

export function parseConversionEvent(raw: string): ConversionEvent | null {
  try {
    const value: unknown = JSON.parse(raw);
    if (typeof value !== 'object' || value === null) return null;
    const candidate = value as Partial<ConversionEvent>;
    if (typeof candidate.sequence !== 'number') return null;
    if (typeof candidate.status !== 'string') return null;
    if (typeof candidate.type !== 'string') return null;
    return {
      sequence: candidate.sequence,
      event_id: candidate.event_id ?? `seq-${candidate.sequence}`,
      conversion_id: candidate.conversion_id ?? '',
      run_id: candidate.run_id ?? '',
      type: candidate.type,
      status: candidate.status,
      stage: candidate.stage ?? 'done',
      progress: typeof candidate.progress === 'number' ? candidate.progress : 0,
      progress_indeterminate: candidate.progress_indeterminate ?? false,
      message: candidate.message ?? '',
      payload: candidate.payload ?? {},
      at: candidate.at ?? new Date().toISOString(),
    };
  } catch {
    return null;
  }
}

/* ------------------------------------------------------------------------- */
/* Hook                                                                       */
/* ------------------------------------------------------------------------- */

export type ConnectionState = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed' | 'error';
export type StreamTransport = 'fetch' | 'eventsource' | null;

export interface UseConversionEventsOptions {
  enabled?: boolean;
  /**
   * The run the page is showing. Events of another run (a previous attempt that
   * the replayed stream still carries) are ignored, and cannot finish the stream.
   */
  runId?: string | null;
  initialLastEventId?: string | null;
  /** How many events to keep in memory (the log shows the newest ones). */
  maxEvents?: number;
  /** Backoff base, exponential: base * 2^attempt, capped at maxDelayMs. */
  baseDelayMs?: number;
  maxDelayMs?: number;
  onEvent?: (event: ConversionEvent) => void;
  /** Called after a reconnect succeeds so the page can re-fetch the truth. */
  onReconnect?: () => void;
}

export interface UseConversionEventsResult {
  events: ConversionEvent[];
  lastEvent: ConversionEvent | null;
  lastEventId: string | null;
  connection: ConnectionState;
  transport: StreamTransport;
  error: string | null;
  reconnectAttempts: number;
}

const MAX_RECONNECT_ATTEMPTS = 8;

/** Adds `run_id` so the server can scope the stream to one run (ignored if unsupported). */
function withRunId(url: string, runId: string | null | undefined): string {
  if (!runId) return url;
  const separator = url.includes('?') ? '&' : '?';
  return `${url}${separator}run_id=${encodeURIComponent(runId)}`;
}

export function useConversionEvents(
  conversionId: string | null | undefined,
  options: UseConversionEventsOptions = {},
): UseConversionEventsResult {
  const {
    enabled = true,
    runId = null,
    initialLastEventId = null,
    maxEvents = 50,
    baseDelayMs = 800,
    maxDelayMs = 15_000,
    onEvent,
    onReconnect,
  } = options;

  const [events, setEvents] = useState<ConversionEvent[]>([]);
  const [lastEvent, setLastEvent] = useState<ConversionEvent | null>(null);
  const [lastEventId, setLastEventId] = useState<string | null>(initialLastEventId);
  const [connection, setConnection] = useState<ConnectionState>('idle');
  const [transport, setTransport] = useState<StreamTransport>(null);
  const [error, setError] = useState<string | null>(null);
  const [reconnectAttempts, setReconnectAttempts] = useState(0);

  const lastIdRef = useRef<string | null>(initialLastEventId);
  const onEventRef = useRef(onEvent);
  const onReconnectRef = useRef(onReconnect);
  const maxEventsRef = useRef(maxEvents);
  onEventRef.current = onEvent;
  onReconnectRef.current = onReconnect;
  maxEventsRef.current = maxEvents;

  // A different conversion, or a new run of the same conversion, starts a new
  // event log: nothing from the previous stream may stay visible.
  useEffect(() => {
    lastIdRef.current = initialLastEventId;
    setLastEventId(initialLastEventId);
    setEvents([]);
    setLastEvent(null);
    setError(null);
    setReconnectAttempts(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversionId, runId]);

  useEffect(() => {
    if (!enabled || !conversionId) {
      setConnection('idle');
      return;
    }

    // Captured per run: a retry changes `runId` and restarts this effect.
    const expectedRunId = runId;

    let cancelled = false;
    let finished = false;
    let openedOnce = false;
    let attempt = 0;
    let timer: number | null = null;
    let controller: AbortController | null = null;
    let source: EventSource | null = null;
    let usingFallback = false;

    const releaseTransport = () => {
      controller?.abort();
      controller = null;
      source?.close();
      source = null;
    };

    const clearTimer = () => {
      if (timer !== null) {
        window.clearTimeout(timer);
        timer = null;
      }
    };

    const scheduleReconnect = (reason: string) => {
      if (cancelled || finished) return;
      clearTimer();
      if (attempt >= MAX_RECONNECT_ATTEMPTS) {
        setConnection('error');
        setError(`${reason} Tentativas de reconexão esgotadas.`);
        return;
      }
      const delay = Math.min(maxDelayMs, baseDelayMs * 2 ** attempt);
      attempt += 1;
      setReconnectAttempts(attempt);
      setConnection('reconnecting');
      setError(reason);
      timer = window.setTimeout(() => {
        timer = null;
        void connectFetch();
      }, delay);
    };

    const handleFrame = (frame: SseFrame) => {
      if (cancelled) return;
      if (frame.event === 'heartbeat' || frame.data === '[DONE]' || frame.data === 'ping') return;

      const parsed = parseConversionEvent(frame.data);
      if (!parsed) return;
      // Another run's event (e.g. the previous attempt's terminal `failed`) must
      // neither be shown nor finish this stream.
      if (expectedRunId && parsed.run_id && parsed.run_id !== expectedRunId) return;

      const nextId = frame.id ?? (Number.isFinite(parsed.sequence) ? String(parsed.sequence) : lastIdRef.current);
      lastIdRef.current = nextId;
      setLastEventId(nextId);
      setEvents((previous) => {
        if (previous.some((item) => item.event_id === parsed.event_id && item.sequence === parsed.sequence)) {
          return previous;
        }
        const next = [...previous, parsed];
        const limit = maxEventsRef.current;
        return next.length > limit ? next.slice(next.length - limit) : next;
      });
      setLastEvent(parsed);
      onEventRef.current?.(parsed);

      if (isTerminal(parsed.status) || parsed.type === 'completed' || parsed.type === 'failed') {
        finished = true;
        clearTimer();
        releaseTransport();
        setConnection('closed');
        setError(null);
      }
    };

    const startEventSource = (reason: string) => {
      if (cancelled || finished) return;
      if (typeof EventSource === 'undefined') {
        scheduleReconnect(reason);
        return;
      }

      usingFallback = true;
      setTransport('eventsource');
      setConnection('reconnecting');
      setError(reason);

      try {
        const es = new EventSource(
          withRunId(
            conversionEventsUrl(conversionId, {
              lastEventId: lastIdRef.current,
              accessToken: resolveToken(),
            }),
            expectedRunId,
          ),
        );
        source = es;

        es.onopen = () => {
          if (cancelled || finished) {
            es.close();
            return;
          }
          setConnection('open');
          setError(null);
          attempt = 0;
          setReconnectAttempts(0);
          if (openedOnce) onReconnectRef.current?.();
          openedOnce = true;
        };

        es.onmessage = (message) => {
          handleFrame({
            id: message.lastEventId || undefined,
            data: typeof message.data === 'string' ? message.data : String(message.data),
          });
        };

        es.onerror = () => {
          es.close();
          source = null;
          if (cancelled || finished) return;
          scheduleReconnect('O stream do servidor foi interrompido.');
        };
      } catch {
        scheduleReconnect(reason);
      }
    };

    const connectFetch = async (): Promise<void> => {
      if (cancelled || finished) return;
      releaseTransport();
      usingFallback = false;
      setTransport('fetch');
      setConnection(openedOnce ? 'reconnecting' : 'connecting');

      const localController = new AbortController();
      controller = localController;

      try {
        const headers: Record<string, string> = {
          ...authHeaders(),
          Accept: 'text/event-stream',
          'Cache-Control': 'no-cache',
        };
        if (lastIdRef.current) headers['Last-Event-ID'] = lastIdRef.current;

        const response = await fetch(
          withRunId(conversionEventsUrl(conversionId, { lastEventId: lastIdRef.current }), expectedRunId),
          {
            method: 'GET',
            headers,
            signal: localController.signal,
            cache: 'no-store',
          },
        );

        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        if (!response.body) throw new Error('O servidor não enviou um stream.');

        setConnection('open');
        setError(null);
        attempt = 0;
        setReconnectAttempts(0);
        if (openedOnce) onReconnectRef.current?.();
        openedOnce = true;

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        const consume = createSseParser(handleFrame);

        while (!cancelled && !finished) {
          const { done, value } = await reader.read();
          if (done) break;
          if (value) consume(decoder.decode(value, { stream: true }));
        }
        await reader.cancel().catch(() => undefined);

        if (!cancelled && !finished) scheduleReconnect('O stream foi encerrado pelo servidor.');
      } catch (cause) {
        if (cancelled || finished) return;
        if (cause instanceof DOMException && cause.name === 'AbortError') return;
        const message = cause instanceof Error ? cause.message : String(cause);
        if (!usingFallback) startEventSource(`Falha no stream (${message}). Tentando EventSource.`);
        else scheduleReconnect(`Falha no stream (${message}).`);
      }
    };

    void connectFetch();

    return () => {
      cancelled = true;
      clearTimer();
      releaseTransport();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversionId, enabled, runId]);

  return {
    events,
    lastEvent,
    lastEventId,
    connection,
    transport,
    error,
    reconnectAttempts,
  };
}
