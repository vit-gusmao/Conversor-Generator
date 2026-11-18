/**
 * Runtime configuration for the MCP adapter.
 *
 * Every value comes from the environment so the host (Claude Desktop, Cursor,
 * a CI runner…) can configure the adapter without touching code. Invalid
 * values fail loudly at startup with a readable message instead of surfacing as
 * a confusing network error later.
 */

import { homedir } from 'node:os';
import path from 'node:path';

export type McpTransport = 'stdio' | 'http';

export interface McpConfig {
  /** Public API root *without* the `/api/v1` suffix, e.g. `http://localhost:8000`. */
  apiBaseUrl: string;
  /** Optional fixed device id. When absent, one is generated and persisted. */
  deviceId: string | null;
  /** Directory holding the identity file and the submission ledger. */
  configDir: string;
  /** Directories a tool may read from / write to. */
  allowedRoots: string[];
  /** Maximum number of concurrent part transfers across every item. */
  transferConcurrency: number;
  transport: McpTransport;
  httpPort: number;
  /** Interface the HTTP transport binds to. Loopback unless explicitly overridden. */
  httpHost: string;
}

export class ConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ConfigError';
  }
}

const DEVICE_ID_PATTERN = /^[A-Za-z0-9._:-]{8,128}$/;
const DEFAULT_CONCURRENCY = 3;
const DEFAULT_HTTP_PORT = 7801;
const DEFAULT_HTTP_HOST = '127.0.0.1';

function read(env: NodeJS.ProcessEnv, key: string): string | undefined {
  const raw = env[key];
  if (raw === undefined) return undefined;
  const trimmed = raw.trim();
  return trimmed.length > 0 ? trimmed : undefined;
}

/** Expands `~` and `~/x`, then resolves against the current directory. */
export function expandPath(value: string): string {
  let expanded = value;
  if (value === '~') expanded = homedir();
  else if (value.startsWith('~/') || value.startsWith('~\\')) {
    expanded = path.join(homedir(), value.slice(2));
  }
  return path.resolve(expanded);
}

function parseApiBaseUrl(raw: string | undefined): string {
  const value = raw ?? 'http://localhost:8000';
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw new ConfigError(`PDF2X_API_BASE_URL is not a valid URL: ${JSON.stringify(value)}`);
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new ConfigError(
      `PDF2X_API_BASE_URL must use http or https, received ${parsed.protocol}`,
    );
  }
  if (parsed.username || parsed.password) {
    throw new ConfigError('PDF2X_API_BASE_URL must not embed credentials.');
  }
  if (parsed.search || parsed.hash) {
    throw new ConfigError('PDF2X_API_BASE_URL must not contain a query string or fragment.');
  }

  // Accept both `http://host:port` and `http://host:port/api/v1`: the API root
  // is stored canonically and `/api/v1` is appended exactly once.
  let pathname = parsed.pathname.replace(/\/+$/, '');
  if (pathname.toLowerCase().endsWith('/api/v1')) {
    pathname = pathname.slice(0, -'/api/v1'.length);
  }
  if (pathname !== '' && pathname !== '/') {
    throw new ConfigError(
      `PDF2X_API_BASE_URL must be the server root (optionally ending in /api/v1), received path ${pathname}`,
    );
  }
  return `${parsed.protocol}//${parsed.host}`;
}

function parseDeviceId(raw: string | undefined): string | null {
  if (raw === undefined) return null;
  if (!DEVICE_ID_PATTERN.test(raw)) {
    throw new ConfigError(
      'PDF2X_DEVICE_ID must be 8-128 characters of A-Z, a-z, 0-9, dot, underscore, colon or hyphen.',
    );
  }
  return raw;
}

function parseAllowedRoots(raw: string | undefined): string[] {
  const source = raw && raw.trim().length > 0 ? raw : homedir();
  const roots = source
    .split(',')
    .map((value) => value.trim())
    .filter((value) => value.length > 0)
    .map((value) => expandPath(value));

  if (roots.length === 0) {
    throw new ConfigError('PDF2X_ALLOWED_ROOTS must list at least one directory.');
  }
  for (const root of roots) {
    if (!path.isAbsolute(root)) {
      throw new ConfigError(`PDF2X_ALLOWED_ROOTS entry is not an absolute path: ${root}`);
    }
  }
  // Deduplicate while keeping the first occurrence.
  return [...new Set(roots)];
}

function parseInteger(
  name: string,
  raw: string | undefined,
  fallback: number,
  min: number,
  max: number,
): number {
  if (raw === undefined) return fallback;
  if (!/^\d+$/.test(raw)) {
    throw new ConfigError(`${name} must be an integer between ${min} and ${max}, received ${JSON.stringify(raw)}`);
  }
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < min || value > max) {
    throw new ConfigError(`${name} must be an integer between ${min} and ${max}, received ${raw}`);
  }
  return value;
}

/** Builds a validated configuration from the environment. */
export function loadConfig(env: NodeJS.ProcessEnv = process.env): McpConfig {
  const transportRaw = read(env, 'PDF2X_MCP_TRANSPORT') ?? 'stdio';
  if (transportRaw !== 'stdio' && transportRaw !== 'http') {
    throw new ConfigError(
      `PDF2X_MCP_TRANSPORT must be "stdio" or "http", received ${JSON.stringify(transportRaw)}`,
    );
  }

  const configDirRaw = read(env, 'PDF2X_CONFIG_DIR');
  const configDir = configDirRaw ? expandPath(configDirRaw) : path.join(homedir(), '.pdf2x');
  if (!path.isAbsolute(configDir)) {
    throw new ConfigError('PDF2X_CONFIG_DIR must resolve to an absolute path.');
  }

  return {
    apiBaseUrl: parseApiBaseUrl(read(env, 'PDF2X_API_BASE_URL')),
    deviceId: parseDeviceId(read(env, 'PDF2X_DEVICE_ID')),
    configDir,
    allowedRoots: parseAllowedRoots(read(env, 'PDF2X_ALLOWED_ROOTS')),
    transferConcurrency: parseInteger(
      'PDF2X_TRANSFER_CONCURRENCY',
      read(env, 'PDF2X_TRANSFER_CONCURRENCY'),
      DEFAULT_CONCURRENCY,
      1,
      32,
    ),
    transport: transportRaw,
    httpPort: parseInteger(
      'PDF2X_MCP_PORT',
      read(env, 'PDF2X_MCP_PORT'),
      DEFAULT_HTTP_PORT,
      1,
      65535,
    ),
    httpHost: read(env, 'PDF2X_MCP_HOST') ?? DEFAULT_HTTP_HOST,
  };
}
