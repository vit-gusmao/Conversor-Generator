import { existsSync, mkdtempSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import {
  IdentityError,
  identityDirectory,
  loadOrCreateDeviceId,
  loadOrCreateJwtSecret,
  resolveOwnerId,
} from '../src/identity';

function appData(): string {
  return mkdtempSync(join(tmpdir(), 'desktop-identity-'));
}

describe('per-install JWT secret (plan item 1)', () => {
  it('creates a random secret of at least 64 hex characters and keeps it across starts', () => {
    const dir = appData();
    const first = loadOrCreateJwtSecret(dir);
    expect(first).toMatch(/^[0-9a-f]{96}$/);
    expect(first).not.toBe('dev-jwt-secret-change-me');
    expect(loadOrCreateJwtSecret(dir)).toBe(first);
  });

  it('stores it under identity/ and never in another place', () => {
    const dir = appData();
    loadOrCreateJwtSecret(dir);
    expect(existsSync(join(identityDirectory(dir), 'jwt-secret'))).toBe(true);
  });

  it('replaces a corrupt file and keeps the bad bytes for diagnosis', () => {
    const dir = appData();
    const file = join(identityDirectory(dir), 'jwt-secret');
    loadOrCreateJwtSecret(dir);
    writeFileSync(file, 'curto');
    const replaced = loadOrCreateJwtSecret(dir);
    expect(replaced).toMatch(/^[0-9a-f]{96}$/);
    expect(readdirSync(identityDirectory(dir)).some((name) => name.startsWith('jwt-secret.corrupt-'))).toBe(true);
  });
});

describe('device id and owner restoration (F4)', () => {
  it('keeps the same device id across restarts', () => {
    const dir = appData();
    const id = loadOrCreateDeviceId(dir);
    expect(id).toMatch(/^[A-Za-z0-9_-]{22,}$/);
    expect(loadOrCreateDeviceId(dir)).toBe(id);
    expect(readFileSync(join(identityDirectory(dir), 'device-id'), 'utf8').trim()).toBe(id);
  });

  it('reports a storage failure instead of silently using a random identity', () => {
    const dir = appData();
    // A file where the directory should be makes the write impossible.
    writeFileSync(identityDirectory(dir), 'não é um diretório');
    expect(() => loadOrCreateDeviceId(dir)).toThrowError(IdentityError);
  });

  it('resolves the same owner after the backend moves to another port', async () => {
    const dir = appData();
    const deviceId = loadOrCreateDeviceId(dir);
    // A fake backend that maps device ids to owners, as the real one does.
    const owners = new Map<string, string>();
    const fakeFetch = (async (url: string, init: { headers: Record<string, string> }) => {
      expect(url).toMatch(/\/api\/v1\/auth\/anonymous$/);
      expect(init.headers['x-desktop-host']).toBeTruthy();
      const device = init.headers['x-device-id'];
      if (!owners.has(device)) owners.set(device, `usr_${owners.size + 1}`);
      return new Response(JSON.stringify({ user: { id: owners.get(device) } }), { status: 201 });
    }) as unknown as typeof fetch;

    const before = await resolveOwnerId({
      baseUrl: 'http://127.0.0.1:40001',
      deviceId,
      hostSecret: 'secret-a',
      fetchImpl: fakeFetch,
    });
    const after = await resolveOwnerId({
      baseUrl: 'http://127.0.0.1:40002',
      deviceId: loadOrCreateDeviceId(dir),
      hostSecret: 'secret-b',
      fetchImpl: fakeFetch,
    });
    expect(after).toBe(before);
  });

  it('fails clearly when the backend does not establish the session', async () => {
    await expect(
      resolveOwnerId({
        baseUrl: 'http://127.0.0.1:40001',
        deviceId: 'device-abcdefghijklmnopqrstu',
        hostSecret: 'secret',
        fetchImpl: (async () => new Response('{}', { status: 403 })) as unknown as typeof fetch,
      }),
    ).rejects.toMatchObject({ code: 'IDENTITY_UNAVAILABLE' });
  });
});
