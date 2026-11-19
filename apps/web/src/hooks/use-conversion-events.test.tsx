import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import type { ConversionEvent } from '@pdf2x/contracts';
import {
  createSseParser,
  parseSseFrame,
  useConversionEvents,
} from '@/hooks/use-conversion-events';

function makeEvent(sequence: number): ConversionEvent {
  return {
    sequence,
    event_id: `evt-${sequence}`,
    conversion_id: 'conv-1',
    run_id: 'run-1',
    type: 'progress',
    status: 'extracting',
    stage: 'extract',
    progress: 0.2 + sequence / 100,
    progress_indeterminate: false,
    message: `evento ${sequence}`,
    payload: { sequence },
    at: new Date().toISOString(),
  };
}

function streamResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return { ok: true, status: 200, headers: new Headers(), body } as unknown as Response;
}

/** A stream that stays open so the hook does not schedule another reconnect. */
function openResponse(): Response {
  const body = new ReadableStream<Uint8Array>({
    start() {
      // intentionally never closes
    },
  });
  return { ok: true, status: 200, headers: new Headers(), body } as unknown as Response;
}

describe('SSE framing', () => {
  it('parses id, event and data fields', () => {
    expect(parseSseFrame('id: 7\nevent: status\ndata: {"a":1}')).toEqual({
      id: '7',
      event: 'status',
      data: '{"a":1}',
    });
  });

  it('joins multi-line data and ignores comments', () => {
    const frame = parseSseFrame(': keep-alive\ndata: linha 1\ndata: linha 2');
    expect(frame?.data).toBe('linha 1\nlinha 2');
  });

  it('buffers partial chunks until a blank line arrives', () => {
    const received: string[] = [];
    const parse = createSseParser((frame) => received.push(frame.data));

    parse('data: {"sequence":1}');
    expect(received).toHaveLength(0);
    parse('\n\n');
    expect(received).toEqual(['{"sequence":1}']);

    parse('data: 1\r\n\r\ndata: 2\r\n\r\n');
    expect(received).toEqual(['{"sequence":1}', '1', '2']);
  });
});

describe('useConversionEvents', () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal('fetch', fetchMock);
    fetchMock.mockReset();
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('streams frames with fetch and resumes from the last cursor after a disconnect', async () => {
    const firstFrame = `id: 1\ndata: ${JSON.stringify(makeEvent(1))}\n\n`;
    const secondRaw = `data: ${JSON.stringify(makeEvent(2))}\n\n`;

    fetchMock.mockResolvedValueOnce(
      streamResponse([firstFrame + secondRaw.slice(0, 12), secondRaw.slice(12)]),
    );
    fetchMock.mockResolvedValue(openResponse());

    const { result, unmount } = renderHook(() =>
      useConversionEvents('conv-1', { baseDelayMs: 5, maxDelayMs: 20 }),
    );

    await waitFor(() => {
      expect(result.current.events).toHaveLength(2);
    });
    expect(result.current.events.map((event) => event.sequence)).toEqual([1, 2]);
    expect(result.current.lastEvent?.message).toBe('evento 2');

    await waitFor(() => {
      expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2);
    });

    const [firstUrl] = fetchMock.mock.calls[0]!;
    expect(String(firstUrl)).toContain('/api/v1/conversions/conv-1/events');
    expect(String(firstUrl)).not.toContain('last_event_id');

    const [secondUrl, secondInit] = fetchMock.mock.calls[1]!;
    expect(String(secondUrl)).toContain('last_event_id=2');
    const headers = new Headers(secondInit?.headers);
    expect(headers.get('Last-Event-ID')).toBe('2');
    expect(headers.get('Accept')).toBe('text/event-stream');

    await waitFor(() => {
      expect(result.current.connection).toBe('open');
    });

    unmount();
  });

  it('ignores a previous run terminal failure after a retry and scopes the stream to the new run', async () => {
    const previousFailure = {
      ...makeEvent(1),
      run_id: 'run-1',
      type: 'failed',
      status: 'failed' as const,
      stage: 'failed',
      message: 'falhou na primeira tentativa',
    };
    const currentQueued = { ...makeEvent(2), run_id: 'run-2', type: 'status', status: 'queued' as const };

    fetchMock.mockResolvedValueOnce(
      streamResponse([
        `id: 1
data: ${JSON.stringify(previousFailure)}

`,
        `id: 2
data: ${JSON.stringify(currentQueued)}

`,
      ]),
    );
    fetchMock.mockResolvedValue(openResponse());

    const { result, unmount } = renderHook(() =>
      useConversionEvents('conv-1', { runId: 'run-2', baseDelayMs: 5, maxDelayMs: 20 }),
    );

    await waitFor(() => {
      expect(result.current.events).toHaveLength(1);
    });
    expect(result.current.events[0]?.run_id).toBe('run-2');
    expect(result.current.lastEvent?.status).toBe('queued');
    // The previous run's terminal event did not close the stream.
    expect(result.current.connection).not.toBe('closed');

    const [firstUrl] = fetchMock.mock.calls[0]!;
    expect(String(firstUrl)).toContain('run_id=run-2');

    unmount();
  });

  it('does not open a stream when disabled', async () => {
    renderHook(() => useConversionEvents('conv-1', { enabled: false }));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
