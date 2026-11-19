/**
 * Backend 'stopped' handling in the Electron main process (doc 03 §4).
 *
 * The real main module runs against mocked `electron` and supervisor modules:
 * an exit after READY is recovered once automatically, then the user is asked
 * to restart or quit, and deliberate restarts are never treated as crashes.
 */

import { EventEmitter } from 'node:events';

import { app, dialog } from 'electron';
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

const shared = vi.hoisted(() => ({
  instances: [] as Array<EventEmitter & Record<string, unknown>>,
  windows: [] as Array<{ loadURL: ReturnType<typeof vi.fn> }>,
}));

vi.mock('electron', () => {
  const dialogMock = { showMessageBox: vi.fn(), showErrorBox: vi.fn(), showOpenDialog: vi.fn() };
  class BrowserWindowMock {
    webContents = { on: vi.fn(), setWindowOpenHandler: vi.fn() };
    loadURL = vi.fn(async () => undefined);
    once = vi.fn();
    on = vi.fn();
    show = vi.fn();
    hide = vi.fn();
    focus = vi.fn();
    isVisible = vi.fn(() => true);
    isDestroyed = vi.fn(() => false);

    constructor() {
      shared.windows.push(this);
    }
  }
  class TrayMock {
    setToolTip = vi.fn();
    setContextMenu = vi.fn();
    on = vi.fn();
  }
  return {
    app: {
      setName: vi.fn(),
      requestSingleInstanceLock: vi.fn(() => true),
      on: vi.fn(),
      whenReady: vi.fn(() => Promise.resolve()),
      getPath: vi.fn(() => process.env.TEMP ?? '.'),
      quit: vi.fn(),
    },
    session: {
      defaultSession: {
        webRequest: { onBeforeSendHeaders: vi.fn(), onHeadersReceived: vi.fn() },
      },
    },
    BrowserWindow: BrowserWindowMock,
    Tray: TrayMock,
    dialog: dialogMock,
    ipcMain: { handle: vi.fn() },
    Menu: { buildFromTemplate: vi.fn(() => ({})) },
    nativeImage: { createEmpty: vi.fn(() => ({})) },
  };
});

vi.mock('../src/backend-supervisor', async () => {
  const { EventEmitter: Emitter } = await import('node:events');
  class BackendSupervisorMock extends Emitter {
    static newToken(): string {
      return 'mock-token-for-tests-0123456789-abcdefghijklmnopqrstuvwxyz';
    }

    readonly internalToken = 'mock-token-for-tests-0123456789-abcdefghijklmnopqrstuvwxyz';
    readonly bootstrapSecret = 'mock-host-secret-0123456789-abcdefghijklmnopqrstuvwxyz';
    start = vi.fn(async () => ({
      port: 40001,
      baseUrl: 'http://127.0.0.1:40001',
      bootId: 'boot-1',
      version: '1.0.0',
    }));
    restart = vi.fn(async () => ({
      port: 40002,
      baseUrl: 'http://127.0.0.1:40002',
      bootId: 'boot-2',
      version: '1.0.0',
    }));
    stop = vi.fn(async () => undefined);

    constructor() {
      super();
      shared.instances.push(this as unknown as EventEmitter & Record<string, unknown>);
    }
  }
  return { BackendSupervisor: BackendSupervisorMock };
});

type SupervisorDouble = EventEmitter & {
  start: ReturnType<typeof vi.fn>;
  restart: ReturnType<typeof vi.fn>;
};

function supervisor(): SupervisorDouble {
  return shared.instances[0] as unknown as SupervisorDouble;
}

async function flush(): Promise<void> {
  for (let i = 0; i < 5; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

describe('main process: backend stopped handling', () => {
  beforeAll(async () => {
    await import('../src/main');
    await vi.waitFor(() => expect(shared.instances).toHaveLength(1));
    await vi.waitFor(() => {
      expect(shared.windows.length).toBeGreaterThan(0);
      expect(shared.windows[0].loadURL).toHaveBeenCalledWith('http://127.0.0.1:40001/');
    });
  });

  beforeEach(() => {
    vi.mocked(dialog.showMessageBox).mockClear();
    vi.mocked(app.quit).mockClear();
    supervisor().restart.mockClear();
    shared.windows[0].loadURL.mockClear();
  });

  it('restarts automatically once after a crash and reloads the new origin', async () => {
    supervisor().emit('stopped', 1);
    await vi.waitFor(() => expect(supervisor().restart).toHaveBeenCalledTimes(1));
    await vi.waitFor(() =>
      expect(shared.windows[0].loadURL).toHaveBeenLastCalledWith('http://127.0.0.1:40002/'),
    );
    await flush();
    expect(dialog.showMessageBox).not.toHaveBeenCalled();
    expect(app.quit).not.toHaveBeenCalled();
  });

  it('focuses the existing window when a second instance is launched', async () => {
    const handler = vi
      .mocked(app.on)
      .mock.calls.find((call) => call[0] === 'second-instance')?.[1] as (() => void) | undefined;
    expect(handler).toBeTypeOf('function');
    const window = shared.windows[0] as unknown as {
      show: ReturnType<typeof vi.fn>;
      focus: ReturnType<typeof vi.fn>;
      isVisible: ReturnType<typeof vi.fn>;
    };
    window.focus.mockClear();
    window.show.mockClear();

    window.isVisible.mockReturnValueOnce(true);
    handler?.();
    expect(window.focus).toHaveBeenCalledTimes(1);
    expect(window.show).not.toHaveBeenCalled();

    window.isVisible.mockReturnValueOnce(false);
    handler?.();
    expect(window.show).toHaveBeenCalledTimes(1);
    expect(window.focus).toHaveBeenCalledTimes(2);
    // A second launch never starts another backend.
    expect(shared.instances).toHaveLength(1);
  });

  it('ignores the exit of its own deliberate restart', async () => {
    // The automatic budget is spent: the crash opens the dialog. The user picks
    // "Reiniciar backend", and the old child's exit fires during that restart.
    // It must not open another dialog.
    vi.mocked(dialog.showMessageBox).mockResolvedValueOnce({ response: 0, checkboxChecked: false });
    supervisor().restart.mockImplementationOnce(async () => {
      supervisor().emit('stopped', 0);
      return { port: 40003, baseUrl: 'http://127.0.0.1:40003', bootId: 'boot-3', version: '1.0.0' };
    });

    supervisor().emit('stopped', 1);
    await vi.waitFor(() => expect(supervisor().restart).toHaveBeenCalledTimes(1));
    await flush();
    expect(dialog.showMessageBox).toHaveBeenCalledTimes(1);
    expect(app.quit).not.toHaveBeenCalled();
  });

  it('asks again when a restart fails, and quits on request with the log dir in the message', async () => {
    vi.mocked(dialog.showMessageBox)
      .mockResolvedValueOnce({ response: 0, checkboxChecked: false })
      .mockResolvedValueOnce({ response: 1, checkboxChecked: false });
    supervisor().restart.mockRejectedValueOnce(new Error('backend nao iniciou'));

    supervisor().emit('stopped', 2);
    await vi.waitFor(() => expect(app.quit).toHaveBeenCalledTimes(1));

    expect(dialog.showMessageBox).toHaveBeenCalledTimes(2);
    const options = vi.mocked(dialog.showMessageBox).mock.calls[0][0] as unknown as {
      buttons: string[];
      detail: string;
    };
    expect(options.buttons).toEqual(['Reiniciar backend', 'Sair']);
    expect(options.detail).toContain('logs');
  });
});
