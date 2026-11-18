/**
 * SSE subscription for one batch.
 *
 * Same framing/Last-Event-ID approach as `use-conversion-events`, but when the
 * stream fails there is no EventSource fallback for batches: the client polls
 * the batch resource every 5 s (with exponential backoff) and lets react-query
 * refetch the authoritative state through `onReconnect`.
 */

import { useEffect, useRef, useState } from 'react';
import type { BatchEvent, BatchItemStatus, BatchStatus } from '@pdf2x/contracts';
import { BATCH_ITEM_STATUSES, BATCH_STATUSES } from '@pdf2x/contracts';
import { authHeaders } from '@/lib/api';
import { batchEventsUrl } from '@/services/batches';
import { createSseParser, type ConnectionState, type SseFrame } from '@/hooks/use-conversion-events';

const MAX_RECONNECT_ATTEMPTS = 8;

const TERMINAL_BATCH_STATUSES: readonly BatchStatus[] = [
  'completed',
  'completed_with_errors',
  'cancelled',
];

const BATCH_EVENT_TYPES: readonly BatchEvent['type'][] = [
  'batch',
  'item',
  'completed',
  'failed',
  'heartbeat',
  'reset',
];

function isBatchStatus(value: unknown): value is BatchStatus {
  return typeof value === 'string' && (BATCH_STATUSES as readonly string[]).includes(value);
}

function isBatchItemStatus(value: unknown): value is BatchItemStatus {
  return typeof value === 'string' && (BATCH_ITEM_STATUSES as readonly string[]).includes(value);
}

function isBatchEventType(value: unknown): value is BatchEvent['type'] {
  return typeof value === 'string' && (BATCH_EVENT_TYPES as readonly string[]).includes(value);
}

export function isTerminalBatchStatus(status: BatchStatus): boolean {
  return TERMINAL_BATCH_STATUSES.includes(status);
}

export function parseBatchEvent(raw: string): BatchEvent | null {
  try {
    const value: unknown = JSON.parse(raw);
    if (typeof value !== 'object' || value === null) return null;
    const candidate = value as Partial<BatchEvent>;
    if (typeof candidate.sequence !== 'number') return null;
    if (!isBatchStatus(candidate.status)) return null;
    if (!isBatchEventType(candidate.type)) return null;
    return {
      batch_id: candidate.batch_id ?? '',
      sequence: candidate.sequence,
      type: candidate.type,
      item_id: candidate.item_id ?? null,
      status: candidate.status,
      item_status: isBatchItemStatus(candidate.item_status) ? candidate.item_status : null,
      message: candidate.message ?? '',
      payload: candidate.payload ?? {},
      at: candidate.at ?? new Date().toISOString(),
    };
  } catch {
    return null;
  }
}

export type BatchStreamTransport = 'fetch' | 'poll' | null;

export interface UseBatchEventsOptions {
  enabled?: boolean;
  initialLastEventId?: string | null;
  /** How many events to keep in memory (the log shows the newest ones). */
  maxEvents?: number;
  /** First retry delay in ms; doubles on every attempt. */
  baseDelayMs?: number;
  maxDelayMs?: number;
  onEvent?: (event: BatchEvent) => void;
  /** Called after a successful reconnect so the page can re-fetch the truth. */
  onReconnect?: () => void;
}

export interface UseBatchEventsResult {
  events: BatchEvent[];
  lastEvent: BatchEvent | null;
  lastEventId: string | null;
  connection: ConnectionState;
  transport: BatchStreamTransport;
  error: string | null;
  reconnectAttempts: number;
}

export function useBatchEvents(
  batchId: string | null | undefined,
  options: UseBatchEventsOptions = {},
): UseBatchEventsResult {
  const {
    enabled = true,
    initialLastEventId = null,
    maxEvents = 50,
    baseDelayMs = 5_000,
    maxDelayMs = 60_000,
    onEvent,
    onReconnect,
  } = options;

  const [events, setEvents] = useState<BatchEvent[]>([]);
  const [lastEvent, setLastEvent] = useState<BatchEvent | null>(null);
  const [lastEventId, setLastEventId] = useState<string | null>(initialLastEventId);
  const [connection, setConnection] = useState<ConnectionState>('idle');
  const [transport, setTransport] = useState<BatchStreamTransport>(null);
  const [error, setError] = useState<string | null>(null);
  const [reconnectAttempts, setReconnectAttempts] = useState(0);

  const lastIdRef = useRef<string | null>(initialLastEventId);
  const onEventRef = useRef(onEvent);
  const onReconnectRef = useRef(onReconnect);
  const maxEventsRef = useRef(maxEvents);
  onEventRef.current = onEvent;
  onReconnectRef.current = onReconnect;
  maxEventsRef.current = maxEvents;

  // A different batch means a different event stream.
  useEffect(() => {
    lastIdRef.current = initialLastEventId;
    setLastEventId(initialLastEventId);
    setEvents([]);
    setLastEvent(null);
    setError(null);
    setReconnectAttempts(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batchId]);

  useEffect(() => {
    if (!enabled || !batchId) {
      setConnection('idle');
      return;
    }

    let cancelled = false;
    let finished = false;
    let openedOnce = false;
    let attempt = 0;
    let timer: number | null = null;
    let controller: AbortController | null = null;

    const releaseTransport = () => {
      controller?.abort();
      controller = null;
    };

    const clearTimer = () => {
      if (timer !== null) {
        window.clearTimeout(timer);
        timer = null;
      }
    };

    const schedulePoll = (reason: string) => {
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
      setTransport('poll');
      setError(reason);
      timer = window.setTimeout(() => {
        timer = null;
        void connectFetch();
      }, delay);
    };

    const handleFrame = (frame: SseFrame) => {
      if (cancelled || finished) return;
      if (frame.event === 'heartbeat' || frame.data === '[DONE]' || frame.data === 'ping') return;

      const parsed = parseBatchEvent(frame.data);
      if (!parsed) return;

      const nextId = frame.id ?? String(parsed.sequence);
      lastIdRef.current = nextId;
      setLastEventId(nextId);
      setEvents((previous) => {
        if (previous.some((item) => item.sequence === parsed.sequence)) return previous;
        const next = [...previous, parsed];
        const limit = maxEventsRef.current;
        return next.length > limit ? next.slice(next.length - limit) : next;
      });
      setLastEvent(parsed);
      onEventRef.current?.(parsed);

      if (
        isTerminalBatchStatus(parsed.status) ||
        parsed.type === 'completed' ||
        parsed.type === 'failed'
      ) {
        finished = true;
        clearTimer();
        releaseTransport();
        setConnection('closed');
        setError(null);
      }
    };

    const connectFetch = async (): Promise<void> => {
      if (cancelled || finished) return;
      releaseTransport();
      setConnection(openedOnce ? 'reconnecting' : 'connecting');
      setTransport('fetch');

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
          batchEventsUrl(batchId, { lastEventId: lastIdRef.current }),
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

        if (!cancelled && !finished) schedulePoll('O stream foi encerrado pelo servidor.');
      } catch (cause) {
        if (cancelled || finished) return;
        if (cause instanceof DOMException && cause.name === 'AbortError') return;
        const message = cause instanceof Error ? cause.message : String(cause);
        // No event stream: fall back to periodic polling of the batch/items.
        onReconnectRef.current?.();
        schedulePoll(`Falha no stream (${message}). Consultando o lote periodicamente.`);
      }
    };

    void connectFetch();

    return () => {
      cancelled = true;
      clearTimer();
      releaseTransport();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batchId, enabled]);

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
