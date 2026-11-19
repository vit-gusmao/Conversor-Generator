/** Typed API client for `/api/v1`. */

import { desktopDeviceId } from './platform';

export const DEFAULT_API_BASE = '/api/v1';

export class ApiError extends Error {
  readonly code: string;
  readonly details: unknown;
  readonly status: number;

  constructor(input: { code: string; message: string; details?: unknown; status: number }) {
    super(input.message);
    this.name = 'ApiError';
    this.code = input.code;
    this.details = input.details ?? null;
    this.status = input.status;
  }
}

export interface ApiRequestOptions extends Omit<RequestInit, 'body'> {
  /** JSON body — sets Content-Type automatically. */
  json?: unknown;
  body?: BodyInit | null;
  /** Explicit bearer token, overriding the registered getter. */
  token?: string | null;
  query?: Record<string, string | number | boolean | null | undefined>;
  /** Cancels the request; an aborted request rejects with the DOMException AbortError. */
  signal?: AbortSignal | null;
}

type TokenGetter = () => string | null | undefined;

let tokenGetter: TokenGetter = () => null;

/** Registered by the auth store so every request carries the bearer token. */
export function setAuthTokenGetter(getter: TokenGetter): void {
  tokenGetter = getter;
}

export function getApiBase(): string {
  const configured = import.meta.env?.VITE_API_BASE;
  const base = (configured && configured.trim()) || DEFAULT_API_BASE;
  return base.endsWith('/') ? base.slice(0, -1) : base;
}

const DEVICE_ID_KEY = 'pdf2x.device_id';
const DEVICE_COOKIE = 'pdf2x_device';
const PERSISTED_AUTH_KEY = 'pdf2x.auth';

function safeLocalStorage(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

function readCookie(name: string): string | null {
  try {
    if (typeof document === 'undefined') return null;
    const prefix = `${name}=`;
    for (const part of document.cookie.split(';')) {
      const trimmed = part.trim();
      if (trimmed.startsWith(prefix)) {
        return decodeURIComponent(trimmed.slice(prefix.length));
      }
    }
  } catch {
    return null;
  }
  return null;
}

function writeCookie(name: string, value: string): void {
  try {
    if (typeof document === 'undefined') return;
    document.cookie = `${name}=${encodeURIComponent(value)}; path=/; max-age=31536000; samesite=lax`;
  } catch {
    // cookies may be disabled; localStorage still keeps the session
  }
}

/**
 * Local identity, owned by this browser. It is stored in localStorage *and* a
 * cookie so clearing one of them does not hand the user a new empty workspace.
 * The server also keeps an HttpOnly cookie for headerless requests.
 */
export function getOrCreateDeviceId(): string {
  // Desktop: the host owns the identity (shared with its granted roots, F4).
  // localStorage and cookies are not consulted or written in that case.
  const fromDesktop = desktopDeviceId();
  if (fromDesktop) return fromDesktop;

  const storage = safeLocalStorage();
  const existing = storage?.getItem(DEVICE_ID_KEY) || readCookie(DEVICE_COOKIE);
  if (existing) {
    storage?.setItem(DEVICE_ID_KEY, existing);
    writeCookie(DEVICE_COOKIE, existing);
    return existing;
  }

  let generated = '';
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      generated = crypto.randomUUID();
    }
  } catch {
    generated = '';
  }
  if (!generated) {
    generated = `dev-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  }
  storage?.setItem(DEVICE_ID_KEY, generated);
  writeCookie(DEVICE_COOKIE, generated);
  return generated;
}

function tokenFromPersistedStore(): string | null {
  const storage = safeLocalStorage();
  const raw = storage?.getItem(PERSISTED_AUTH_KEY);
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== 'object' || parsed === null) return null;
    const state = (parsed as { state?: unknown }).state;
    if (typeof state !== 'object' || state === null) return null;
    const token = (state as { token?: unknown }).token;
    return typeof token === 'string' && token.length > 0 ? token : null;
  } catch {
    return null;
  }
}

export function resolveToken(explicit?: string | null): string | null {
  if (explicit !== undefined) {
    return explicit && explicit.length > 0 ? explicit : null;
  }
  const fromGetter = tokenGetter();
  if (fromGetter) return fromGetter;
  return tokenFromPersistedStore();
}

export function buildUrl(
  path: string,
  query?: ApiRequestOptions['query'],
): string {
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  const base = `${getApiBase()}${normalizedPath}`;
  if (!query) return base;

  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue;
    params.set(key, String(value));
  }
  const qs = params.toString();
  return qs ? `${base}?${qs}` : base;
}

/** Headers used for every authenticated request, including the SSE stream. */
export function authHeaders(token?: string | null): Record<string, string> {
  const headers: Record<string, string> = {
    Accept: 'application/json',
    'x-device-id': getOrCreateDeviceId(),
  };
  const resolved = resolveToken(token);
  if (resolved) headers.Authorization = `Bearer ${resolved}`;
  return headers;
}

async function parseErrorResponse(response: Response): Promise<ApiError> {
  const status = response.status;
  const text = await response.text().catch(() => '');
  if (text) {
    try {
      const parsed: unknown = JSON.parse(text);
      if (typeof parsed === 'object' && parsed !== null && 'error' in parsed) {
        const error = (parsed as { error?: unknown }).error;
        if (typeof error === 'object' && error !== null) {
          const code = (error as { code?: unknown }).code;
          const message = (error as { message?: unknown }).message;
          const details = (error as { details?: unknown }).details;
          return new ApiError({
            code: typeof code === 'string' && code ? code : `HTTP_${status}`,
            message:
              typeof message === 'string' && message
                ? message
                : `A requisição falhou (HTTP ${status}).`,
            details,
            status,
          });
        }
      }
    } catch {
      // not JSON — fall through to the raw text
    }
  }
  return new ApiError({
    code: `HTTP_${status}`,
    message: text.trim() || `A requisição falhou (HTTP ${status}).`,
    details: null,
    status,
  });
}

async function execute(path: string, options: ApiRequestOptions): Promise<Response> {
  const { json, token, query, headers, body, signal, ...rest } = options;
  const merged = new Headers(authHeaders(token));
  if (headers) {
    new Headers(headers).forEach((value, key) => merged.set(key, value));
  }

  let requestBody = body;
  if (json !== undefined) {
    merged.set('Content-Type', 'application/json');
    requestBody = JSON.stringify(json);
  }

  let response: Response;
  try {
    response = await fetch(buildUrl(path, query), {
      ...rest,
      headers: merged,
      body: requestBody,
      // Aborting this signal rejects with an AbortError (rethrown below).
      signal,
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause;
    throw new ApiError({
      code: 'NETWORK_ERROR',
      message: 'Não foi possível contatar a API. Verifique sua conexão e tente novamente.',
      details: { cause: cause instanceof Error ? cause.message : String(cause) },
      status: 0,
    });
  }

  if (!response.ok) throw await parseErrorResponse(response);
  return response;
}

export async function apiFetch<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  const response = await execute(path, options);
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  if (!text) return undefined as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiError({
      code: 'INVALID_RESPONSE',
      message: 'A API respondeu com um formato inesperado.',
      details: { body: text.slice(0, 500) },
      status: response.status,
    });
  }
}

export async function apiBlob(path: string, options: ApiRequestOptions = {}): Promise<Blob> {
  const response = await execute(path, options);
  return response.blob();
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError;
}

export function errorMessage(value: unknown): string {
  if (isApiError(value)) return value.message;
  if (value instanceof Error) return value.message;
  return 'Ocorreu um erro inesperado.';
}

export function errorCode(value: unknown): string | null {
  return isApiError(value) ? value.code : null;
}
