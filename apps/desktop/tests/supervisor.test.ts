import { EventEmitter } from 'node:events';
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { describe, expect, it, vi } from 'vitest';

import { BackendSupervisor, SupervisorError } from '../src/backend-supervisor';

class FakeChild extends EventEmitter {
  stdout = new EventEmitter() as EventEmitter & { on: EventEmitter['on'] };
  stderr = new EventEmitter();
  killed = false;

  emitReady(payload: object): void {
    this.stdout.emit('data', Buffer.from(`CONVERTER_READY ${JSON.stringify(payload)}\n`));
  }

  kill(): boolean {
    this.killed = true;
    this.emit('exit', 0);
    return true;
  }
}

function backendDirWithExecutable(): string {
  const dir = mkdtempSync(join(tmpdir(), 'desktop-backend-'));
  writeFileSync(join(dir, process.platform === 'win32' ? 'converter.exe' : 'converter'), 'binary');
  return dir;
}

function makeSupervisor(child: FakeChild, overrides: Partial<ConstructorParameters<typeof BackendSupervisor>[0]> = {}) {
  const appData = mkdtempSync(join(tmpdir(), 'desktop-data-'));
  mkdirSync(join(appData, 'work'), { recursive: true });
  const spawnImpl = vi.fn(() => child) as unknown as typeof import('node:child_process').spawn;
  const supervisor = new BackendSupervisor({
    backendDir: backendDirWithExecutable(),
    appDataDir: appData,
    workDir: join(appData, 'work'),
    logDir: join(appData, 'logs'),
    webDistDir: join(appData, 'web'),
    spawnImpl,
    startupTimeoutMs: 2000,
    jwtSecret: 'j'.repeat(96),
    ...overrides,
  });
  return { supervisor, spawnImpl, appData };
}

describe('BackendSupervisor', () => {
  it('spawns without a shell and passes absolute directories', async () => {
    const child = new FakeChild();
    const { supervisor, spawnImpl } = makeSupervisor(child);
    const pending = supervisor.start();
    child.emitReady({ port: 41234, boot_id: 'boot-1', version: '1.0.0' });
    const endpoint = await pending;

    expect(endpoint.port).toBe(41234);
    expect(endpoint.baseUrl).toBe('http://127.0.0.1:41234');
    const options = (spawnImpl as unknown as { mock: { calls: unknown[][] } }).mock.calls[0][2] as {
      shell: boolean;
      env: Record<string, string>;
    };
    expect(options.shell).toBe(false);
    expect(options.env.CONVERTER_PORT).toBe('0'); // OS assigns the port
    expect(options.env.APP_DATA_DIR).toBeTruthy();
    expect(options.env.WORK_DIR).toBeTruthy();
    expect(options.env.WEB_DIST_DIR).toBeTruthy();
  });

  it('fails with a diagnostic when the executable is missing', async () => {
    const child = new FakeChild();
    const { supervisor } = makeSupervisor(child, { backendDir: join(tmpdir(), 'nao-existe-xyz') });
    await expect(supervisor.start()).rejects.toBeInstanceOf(SupervisorError);
  });

  it('reports STARTUP_TIMEOUT instead of opening a fake window', async () => {
    const child = new FakeChild();
    const { supervisor } = makeSupervisor(child, { startupTimeoutMs: 50 });
    await expect(supervisor.start()).rejects.toMatchObject({ code: 'STARTUP_TIMEOUT' });
  });

  it('surfaces a crash during startup as BACKEND_EXITED', async () => {
    const child = new FakeChild();
    const { supervisor } = makeSupervisor(child);
    const pending = supervisor.start();
    child.emit('exit', 1);
    await expect(pending).rejects.toMatchObject({ code: 'BACKEND_EXITED' });
  });

  it('stop() terminates only the process it created', async () => {
    const child = new FakeChild();
    const { supervisor } = makeSupervisor(child);
    const pending = supervisor.start();
    child.emitReady({ port: 1234, boot_id: 'b', version: '1' });
    await pending;
    await supervisor.stop(1000);
    expect(child.killed).toBe(true);
    expect(supervisor.running).toBe(false);
  });

  it('accepts a different port on restart without losing the profile', async () => {
    const children = [new FakeChild(), new FakeChild()];
    let index = 0;
    const spawnImpl = vi.fn(() => children[index++]) as unknown as typeof import('node:child_process').spawn;
    const { supervisor } = makeSupervisor(children[0], { spawnImpl });

    const first = supervisor.start();
    children[0].emitReady({ port: 41000, boot_id: 'boot-a', version: '1' });
    expect((await first).port).toBe(41000);

    const restarted = supervisor.restart();
    // restart() stops first; the new child only exists after that await.
    await new Promise((resolve) => setTimeout(resolve, 20));
    children[1].emitReady({ port: 42000, boot_id: 'boot-b', version: '1' });
    const endpoint = await restarted;
    expect(endpoint.port).toBe(42000);
    expect(endpoint.bootId).toBe('boot-b');
  });
});

describe('BackendSupervisor — internal token (review fix)', () => {
  it('never spawns the backend with the documented default token', async () => {
    const child = new FakeChild();
    // The constructor refuses before any spawn.
    let error: unknown = null;
    try {
      makeSupervisor(child, { internalToken: 'dev-internal-token-change-me' });
    } catch (thrown) {
      error = thrown;
    }
    expect(error).toMatchObject({ code: 'INSECURE_INTERNAL_TOKEN' });
  });

  it('shares the per-boot token with the backend environment', async () => {
    const child = new FakeChild();
    const { supervisor, spawnImpl } = makeSupervisor(child, { internalToken: 'token-abc-123' });
    const pending = supervisor.start();
    child.emitReady({ port: 40000, boot_id: 'b', version: '1' });
    await pending;
    const options = (spawnImpl as unknown as { mock: { calls: unknown[][] } }).mock.calls[0][2] as {
      env: Record<string, string>;
    };
    expect(options.env.CONVERTER_INTERNAL_TOKEN).toBe('token-abc-123');
    // The host guard secret is a separate, per-start value.
    expect(options.env.DESKTOP_BOOTSTRAP_SECRET).toBeTruthy();
    expect(options.env.DESKTOP_BOOTSTRAP_SECRET).not.toBe('token-abc-123');
    expect(options.env.DESKTOP_BOOTSTRAP_SECRET).toBe(supervisor.bootstrapSecret);
  });

  it('generates a strong token when none is provided', () => {
    const { supervisor } = makeSupervisor(new FakeChild());
    expect(supervisor.internalToken.length).toBeGreaterThan(32);
    expect(supervisor.internalToken).not.toBe('dev-internal-token-change-me');
  });
});

describe('BackendSupervisor — secrets and environment (WP-E)', () => {
  it('refuses the documented JWT default or a short JWT secret before spawning', () => {
    for (const jwtSecret of ['dev-jwt-secret-change-me', 'curto']) {
      let error: unknown = null;
      try {
        makeSupervisor(new FakeChild(), { jwtSecret });
      } catch (thrown) {
        error = thrown;
      }
      expect(error).toMatchObject({ code: 'INSECURE_JWT_SECRET' });
    }
  });

  it('passes the per-install JWT secret to the backend environment', async () => {
    const child = new FakeChild();
    const jwt = 'a'.repeat(96);
    const { supervisor, spawnImpl } = makeSupervisor(child, { jwtSecret: jwt });
    const pending = supervisor.start();
    child.emitReady({ port: 40010, boot_id: 'b', version: '1' });
    await pending;
    const options = (spawnImpl as unknown as { mock: { calls: unknown[][] } }).mock.calls[0][2] as {
      env: Record<string, string>;
    };
    expect(options.env.JWT_SECRET).toBe(jwt);
    expect(options.env.DESKTOP_HOST_ENABLED).toBe('true');
  });

  it('keeps security values even when the profile or a restart option tries to override them', async () => {
    const child = new FakeChild();
    const { supervisor, spawnImpl } = makeSupervisor(child, {
      internalToken: 'token-real-0123456789',
      env: { CONVERTER_INTERNAL_TOKEN: 'profile-token', JWT_SECRET: 'profile-jwt', DESKTOP_HOST_ENABLED: 'false' },
    });
    const pending = supervisor.start({ DESKTOP_BOOTSTRAP_SECRET: 'extra-secret' });
    child.emitReady({ port: 40011, boot_id: 'b', version: '1' });
    await pending;
    const options = (spawnImpl as unknown as { mock: { calls: unknown[][] } }).mock.calls[0][2] as {
      env: Record<string, string>;
    };
    expect(options.env.CONVERTER_INTERNAL_TOKEN).toBe('token-real-0123456789');
    expect(options.env.JWT_SECRET).toBe('j'.repeat(96));
    expect(options.env.DESKTOP_HOST_ENABLED).toBe('true');
    expect(options.env.DESKTOP_BOOTSTRAP_SECRET).not.toBe('extra-secret');
    expect(options.env.DESKTOP_BOOTSTRAP_SECRET).toBe(supervisor.bootstrapSecret);
  });

  it('issues a new bootstrap secret on every spawn', async () => {
    const children = [new FakeChild(), new FakeChild()];
    let index = 0;
    const spawnImpl = vi.fn(() => children[index++]) as unknown as typeof import('node:child_process').spawn;
    const { supervisor } = makeSupervisor(children[0], { spawnImpl });

    const first = supervisor.start();
    children[0].emitReady({ port: 41100, boot_id: 'a', version: '1' });
    await first;
    const firstSecret = supervisor.bootstrapSecret;
    expect(firstSecret).toBeTruthy();

    const restarted = supervisor.restart();
    await new Promise((resolve) => setTimeout(resolve, 20));
    children[1].emitReady({ port: 41101, boot_id: 'b', version: '1' });
    await restarted;
    expect(supervisor.bootstrapSecret).not.toBe(firstSecret);
  });

  it('keeps only a bounded stderr tail in startup diagnostics', async () => {
    const child = new FakeChild();
    const { supervisor } = makeSupervisor(child);
    const pending = supervisor.start();
    // ~200 KB of noise, then a recognisable last line.
    const noise = 'x'.repeat(1024);
    for (let i = 0; i < 200; i += 1) child.stderr.emit('data', Buffer.from(noise));
    child.stderr.emit('data', Buffer.from('ERRO-FINAL-LINHA'));
    child.emit('exit', 3);
    const error = (await pending.catch((thrown: unknown) => thrown)) as Error;
    expect(error).toMatchObject({ code: 'BACKEND_EXITED' });
    expect(error.message).toContain('ERRO-FINAL-LINHA');
    expect(error.message.length).toBeLessThan(1500);
  });
});
