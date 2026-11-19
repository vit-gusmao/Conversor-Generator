import { describe, expect, it } from 'vitest';

import {
  ALL_URLS_FILTER,
  CONTENT_SECURITY_POLICY,
  HOST_HEADER,
  hostRequestHeaders,
  hostResponseHeaders,
  installHostSession,
  isBackendOrigin,
} from '../src/host-session';

const SECRET = 'host-secret-0123456789-abcdefghijklmnopqrstuvwxyz';

function state(baseUrl: string | null, secret: string | null = SECRET) {
  return { baseUrl: () => baseUrl, secret: () => secret };
}

describe('host session: request header (plan item 3)', () => {
  it('adds the host secret only for the current backend origin', () => {
    const current = state('http://127.0.0.1:40001');
    const own = hostRequestHeaders('http://127.0.0.1:40001/api/v1/sources', { Accept: '*/*' }, current);
    expect(own[HOST_HEADER]).toBe(SECRET);
    expect(own.Accept).toBe('*/*');

    for (const foreign of [
      'http://evil.test/',
      'https://127.0.0.1:40001/', // other scheme
      'http://localhost:40001/', // other host name
      'http://127.0.0.1:40002/', // another port
    ]) {
      const headers = hostRequestHeaders(foreign, {}, current);
      expect(headers[HOST_HEADER]).toBeUndefined();
    }
  });

  it('never matches by string prefix: 4000 is not 40001', () => {
    expect(isBackendOrigin('http://127.0.0.1:40001/x', 'http://127.0.0.1:4000')).toBe(false);
    expect(isBackendOrigin('http://127.0.0.1:4000/x', 'http://127.0.0.1:4000')).toBe(true);
  });

  it('follows a port change without re-registration (state read per call)', () => {
    let base = 'http://127.0.0.1:40001';
    let secret: string | null = 'first-secret';
    const dynamic = { baseUrl: () => base, secret: () => secret };
    expect(hostRequestHeaders('http://127.0.0.1:40001/', {}, dynamic)[HOST_HEADER]).toBe('first-secret');

    base = 'http://127.0.0.1:40002';
    secret = 'second-secret';
    expect(hostRequestHeaders('http://127.0.0.1:40001/', {}, dynamic)[HOST_HEADER]).toBeUndefined();
    expect(hostRequestHeaders('http://127.0.0.1:40002/', {}, dynamic)[HOST_HEADER]).toBe('second-secret');
  });

  it('adds nothing while no backend is running', () => {
    const headers = hostRequestHeaders('http://127.0.0.1:40001/', {}, state(null, null));
    expect(headers[HOST_HEADER]).toBeUndefined();
  });
});

describe('host session: response CSP (plan item 4)', () => {
  it('replaces any existing policy on backend responses with the strict one', () => {
    const headers = hostResponseHeaders(
      'http://127.0.0.1:40001/',
      { 'content-type': ['text/html'], 'content-security-policy': ["script-src 'unsafe-eval'"] },
      state('http://127.0.0.1:40001'),
    );
    expect(headers['Content-Security-Policy']).toEqual([CONTENT_SECURITY_POLICY]);
    expect(headers['content-security-policy']).toBeUndefined();
    expect(headers['content-type']).toEqual(['text/html']);
  });

  it('is exactly the documented policy', () => {
    expect(CONTENT_SECURITY_POLICY).toContain("default-src 'self'");
    expect(CONTENT_SECURITY_POLICY).toContain("script-src 'self'");
    expect(CONTENT_SECURITY_POLICY).toContain("style-src 'self' 'unsafe-inline'");
    expect(CONTENT_SECURITY_POLICY).toContain("img-src 'self' data: blob:");
    expect(CONTENT_SECURITY_POLICY).toContain("font-src 'self' data:");
    expect(CONTENT_SECURITY_POLICY).toContain("connect-src 'self'");
    expect(CONTENT_SECURITY_POLICY).toContain("object-src 'none'");
    expect(CONTENT_SECURITY_POLICY).toContain("base-uri 'self'");
    expect(CONTENT_SECURITY_POLICY).toContain("frame-ancestors 'none'");
    expect(CONTENT_SECURITY_POLICY).toContain("form-action 'self'");
    expect(CONTENT_SECURITY_POLICY).not.toContain('*');
  });

  it('leaves foreign responses untouched', () => {
    const original = { 'content-security-policy': ["default-src *"] };
    const headers = hostResponseHeaders('https://cdn.example.test/x.js', original, state('http://127.0.0.1:40001'));
    expect(headers).toBe(original);
  });
});

describe('host session: listener installation', () => {
  it('registers both listeners once, on every URL, and applies them', () => {
    const registered: Record<string, (...args: unknown[]) => void> = {};
    const webRequest = {
      onBeforeSendHeaders: (filter: { urls: string[] }, listener: (...args: unknown[]) => void) => {
        expect(filter.urls).toEqual(ALL_URLS_FILTER);
        registered.before = listener;
      },
      onHeadersReceived: (filter: { urls: string[] }, listener: (...args: unknown[]) => void) => {
        expect(filter.urls).toEqual(ALL_URLS_FILTER);
        registered.received = listener;
      },
    };
    installHostSession(webRequest as never, state('http://127.0.0.1:40001'));

    let sent: { requestHeaders?: Record<string, unknown> } | undefined;
    registered.before({ url: 'http://127.0.0.1:40001/api/v1/x', requestHeaders: {} }, (value: never) => {
      sent = value;
    });
    expect(sent?.requestHeaders?.[HOST_HEADER]).toBe(SECRET);

    let answered: { responseHeaders?: Record<string, unknown> } | undefined;
    registered.received(
      { url: 'http://127.0.0.1:40001/', responseHeaders: {} },
      (value: never) => {
        answered = value;
      },
    );
    expect(answered?.responseHeaders?.['Content-Security-Policy']).toEqual([CONTENT_SECURITY_POLICY]);
  });
});
