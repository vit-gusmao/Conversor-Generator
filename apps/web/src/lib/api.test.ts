import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, apiFetch, buildUrl, setAuthTokenGetter } from '@/lib/api';

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock);
  fetchMock.mockReset();
  setAuthTokenGetter(() => null);
  window.localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('apiFetch', () => {
  it('parses the documented error envelope into ApiError', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ error: { code: 'FILE_TOO_LARGE', message: 'Arquivo grande', details: { max: 10 } } }, 400),
    );

    const error = await apiFetch('/conversions').catch((cause: unknown) => cause);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      code: 'FILE_TOO_LARGE',
      message: 'Arquivo grande',
      details: { max: 10 },
      status: 400,
    });
  });

  it('falls back to the raw body when the error is not JSON', async () => {
    fetchMock.mockResolvedValue(new Response('upstream boom', { status: 500 }));

    const error = await apiFetch('/health').catch((cause: unknown) => cause);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ code: 'HTTP_500', message: 'upstream boom', status: 500 });
  });

  it('injects the bearer token, the device id and JSON headers', async () => {
    window.localStorage.setItem('pdf2x.device_id', 'device-123');
    setAuthTokenGetter(() => 'token-abc');
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }));

    await apiFetch('/conversions', { method: 'POST', json: { hello: 'world' } });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/v1/conversions');
    const headers = new Headers(init?.headers);
    expect(headers.get('Authorization')).toBe('Bearer token-abc');
    expect(headers.get('x-device-id')).toBe('device-123');
    expect(headers.get('Content-Type')).toBe('application/json');
    expect(init?.body).toBe(JSON.stringify({ hello: 'world' }));
  });

  it('does not send Authorization when there is no token', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }));

    await apiFetch('/settings');

    const headers = new Headers(fetchMock.mock.calls[0]?.[1]?.headers);
    expect(headers.has('Authorization')).toBe(false);
    expect(headers.get('x-device-id')).toBeTruthy();
  });

  it('builds query strings and skips empty values', () => {
    expect(buildUrl('/conversions', { page: 2, q: 'nota fiscal', empty: undefined, nil: null })).toBe(
      '/api/v1/conversions?page=2&q=nota+fiscal',
    );
    expect(buildUrl('conversions')).toBe('/api/v1/conversions');
  });

  it('returns undefined for empty successful responses', async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    await expect(apiFetch('/conversions/1')).resolves.toBeUndefined();
  });
});
