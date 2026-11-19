import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { updateSchedule } from '@/services/schedules';
import { updateSource } from '@/services/sources';
import { setAuthTokenGetter } from '@/lib/api';

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(new Response(JSON.stringify({ id: 'x' }), { status: 200 }));
  vi.stubGlobal('fetch', fetchMock);
  setAuthTokenGetter(() => null);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('update verbs match the gateway routes', () => {
  it('sends schedule updates with PUT (declared by both Nest and Python)', async () => {
    await updateSchedule('sch_1', 4, { name: 'Nova' });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(String(url)).toBe('/api/v1/schedules/sch_1');
    expect(init?.method).toBe('PUT');
    expect(JSON.parse(String(init?.body))).toEqual({ name: 'Nova', revision: 4 });
  });

  it('keeps PATCH for source updates (Python declares only PATCH for /sources/:id)', async () => {
    await updateSource('src_1', 2, { name: 'Fonte' });
    const [, init] = fetchMock.mock.calls[0]!;
    expect(init?.method).toBe('PATCH');
  });
});
