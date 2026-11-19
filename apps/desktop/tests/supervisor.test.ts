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
    expect(options.env.DESKTOP_BOOTSTRAP_SECRET).toBe('token-abc-123');
  });

  it('generates a strong token when none is provided', () => {
    const { supervisor } = makeSupervisor(new FakeChild());
    expect(supervisor.internalToken.length).toBeGreaterThan(32);
    expect(supervisor.internalToken).not.toBe('dev-internal-token-change-me');
  });
});
