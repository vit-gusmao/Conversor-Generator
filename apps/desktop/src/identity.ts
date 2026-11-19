/**
 * Per-install identity (plan: "loopback alone is not authentication").
 *
 * - `identity/jwt-secret`: random signing key for local session tokens, never
 *   the documented development default;
 * - `identity/device-id`: the desktop's owner key. The SPA receives the same
 *   value through the preload bridge, so the granted roots and the page share
 *   ONE owner (F4). Both files live outside localStorage and survive a backend
 *   restart on another port.
 *
 * Files are written with an owner-only mode where the platform honours it
 * (POSIX). On Windows the file ACL inherited from the user profile applies.
 */

import { randomBytes } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

export class IdentityError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = 'IdentityError';
    this.code = code;
  }
}

const JWT_FILE = 'jwt-secret';
const DEVICE_FILE = 'device-id';
const JWT_PATTERN = /^[0-9a-f]{96}$/;
const DEVICE_PATTERN = /^[A-Za-z0-9_-]{22,128}$/;

export function identityDirectory(appDataDir: string): string {
  return join(appDataDir, 'identity');
}

function readOrCreate(
  file: string,
  generate: () => string,
  isValid: (value: string) => boolean,
): string {
  if (existsSync(file)) {
    const existing = readFileSync(file, 'utf8').trim();
    if (isValid(existing)) return existing;
    // A corrupt file is replaced; the old bytes are kept for diagnosis.
    renameSync(file, `${file}.corrupt-${Date.now()}`);
  }
  const value = generate();
  const staging = `${file}.tmp-${process.pid}`;
  try {
    mkdirSync(dirname(file), { recursive: true, mode: 0o700 });
    writeFileSync(staging, value, { encoding: 'utf8', mode: 0o600 });
    renameSync(staging, file);
  } catch (error) {
    throw new IdentityError(
      'IDENTITY_STORAGE_FAILED',
      `Não foi possível gravar a identidade local: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  return value;
}

/** 96 hex characters (48 random bytes): well above the 64-character minimum. */
export function loadOrCreateJwtSecret(appDataDir: string): string {
  return readOrCreate(
    join(identityDirectory(appDataDir), JWT_FILE),
    () => randomBytes(48).toString('hex'),
    (value) => JWT_PATTERN.test(value),
  );
}

/** Owner key of this desktop installation. Stable across backend restarts. */
export function loadOrCreateDeviceId(appDataDir: string): string {
  return readOrCreate(
    join(identityDirectory(appDataDir), DEVICE_FILE),
    () => randomBytes(32).toString('base64url'),
    (value) => DEVICE_PATTERN.test(value),
  );
}

export interface OwnerLookupOptions {
  baseUrl: string;
  deviceId: string;
  /** Desktop host secret of the running backend (x-desktop-host). */
  hostSecret: string;
  fetchImpl?: typeof fetch;
}

/**
 * Resolves the owner id the backend derives from the device id. Calling it
 * against a new port after a restart yields the same owner (identity restored).
 */
export async function resolveOwnerId(options: OwnerLookupOptions): Promise<string> {
  const fetchImpl = options.fetchImpl ?? fetch;
  const response = await fetchImpl(`${options.baseUrl}/api/v1/auth/anonymous`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'x-device-id': options.deviceId,
      'x-desktop-host': options.hostSecret,
    },
    body: '{}',
  });
  if (!response.ok) {
    throw new IdentityError('IDENTITY_UNAVAILABLE', 'Não foi possível estabelecer a identidade local.');
  }
  const session = (await response.json()) as { user?: { id?: string } };
  const ownerId = session.user?.id;
  if (!ownerId) {
    throw new IdentityError('IDENTITY_UNAVAILABLE', 'Sessão local sem identificador.');
  }
  return ownerId;
}
