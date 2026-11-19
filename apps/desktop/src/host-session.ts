/**
 * Electron session policy for the backend origin (plan items 3 and 4).
 *
 * - Every request to the CURRENT backend origin gets `x-desktop-host` (the
 *   per-start bootstrap secret). The header is never added for another origin.
 * - Every response from the backend origin gets a strict CSP.
 *
 * Origins are compared exactly (scheme, host and port), never by string
 * prefix: `http://127.0.0.1:4000` must not match `http://127.0.0.1:40001`.
 * The listeners read the current origin and secret on each call, so a backend
 * restart on a new port is covered without re-registering anything.
 */

export const HOST_HEADER = 'x-desktop-host';

export const CONTENT_SECURITY_POLICY = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  "connect-src 'self'",
  "object-src 'none'",
  "base-uri 'self'",
  "frame-ancestors 'none'",
  "form-action 'self'",
].join('; ');

/** Matches every URL; the listener narrows to the backend origin itself. */
export const ALL_URLS_FILTER = ['*://*/*'];

export interface HostSessionState {
  /** Current backend origin (`http://127.0.0.1:<port>`), or null when stopped. */
  baseUrl: () => string | null;
  /** Current bootstrap secret, or null when no backend process is running. */
  secret: () => string | null;
}

type HeaderBag = Record<string, string | string[]>;

export interface WebRequestLike {
  onBeforeSendHeaders(
    filter: { urls: string[] },
    listener: (
      details: { url: string; requestHeaders: HeaderBag },
      callback: (response: { requestHeaders?: HeaderBag }) => void,
    ) => void,
  ): void;
  onHeadersReceived(
    filter: { urls: string[] },
    listener: (
      details: { url: string; responseHeaders?: HeaderBag },
      callback: (response: { responseHeaders?: HeaderBag }) => void,
    ) => void,
  ): void;
}

/** True only when `url` has exactly the origin of `baseUrl`. */
export function isBackendOrigin(url: string, baseUrl: string | null): boolean {
  if (!baseUrl) return false;
  try {
    return new URL(url).origin === new URL(baseUrl).origin;
  } catch {
    return false;
  }
}

/** Request headers with the host secret added (only for the backend origin). */
export function hostRequestHeaders(
  url: string,
  requestHeaders: HeaderBag,
  state: HostSessionState,
): HeaderBag {
  const secret = state.secret();
  if (!secret || !isBackendOrigin(url, state.baseUrl())) {
    return requestHeaders;
  }
  return { ...requestHeaders, [HOST_HEADER]: secret };
}

/** Response headers with the CSP replacing any existing policy (backend only). */
export function hostResponseHeaders(
  url: string,
  responseHeaders: HeaderBag,
  state: HostSessionState,
): HeaderBag {
  if (!isBackendOrigin(url, state.baseUrl())) {
    return responseHeaders;
  }
  const kept: HeaderBag = {};
  for (const [name, value] of Object.entries(responseHeaders)) {
    if (name.toLowerCase() !== 'content-security-policy') kept[name] = value;
  }
  return { ...kept, 'Content-Security-Policy': [CONTENT_SECURITY_POLICY] };
}

/** Registers both listeners once for the session. */
export function installHostSession(webRequest: WebRequestLike, state: HostSessionState): void {
  webRequest.onBeforeSendHeaders({ urls: ALL_URLS_FILTER }, (details, callback) => {
    callback({ requestHeaders: hostRequestHeaders(details.url, details.requestHeaders, state) });
  });
  webRequest.onHeadersReceived({ urls: ALL_URLS_FILTER }, (details, callback) => {
    callback({
      responseHeaders: hostResponseHeaders(details.url, details.responseHeaders ?? {}, state),
    });
  });
}
