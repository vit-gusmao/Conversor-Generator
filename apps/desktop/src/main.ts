/**
 * Electron main process (T12, doc 03 §4/§5/§8).
 *
 * Single instance → profile → backend supervisor → window on the backend's
 * own loopback origin (same origin, relative /api/v1) → tray. The renderer is
 * sandboxed, context-isolated and cannot navigate away from the app.
 */

import { app, BrowserWindow, dialog, ipcMain, Menu, Tray, nativeImage } from 'electron';
import { join } from 'node:path';

import { BackendSupervisor, type RuntimeEndpoint } from './backend-supervisor';
import {
  IPC_CHANNELS,
  IpcError,
  assertSelectionShape,
  assertTrustedSender,
  validateApplyArgs,
  validateModelPickArgs,
  validateSourcePickArgs,
  type IpcEventLike,
} from './ipc-handlers';
import { LocalGrants } from './local-grants';
import { ProfileStore, validateDraft } from './runtime-profile';

const APP_NAME = 'Conversor Generator';

app.setName(APP_NAME);

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let supervisor: BackendSupervisor | null = null;
let endpoint: RuntimeEndpoint | null = null;
let profileStore: ProfileStore | null = null;
let grants: LocalGrants | null = null;
let quitting = false;

function appDataDir(): string {
  return join(app.getPath('userData'));
}

function resourcesDir(): string {
  return app.isPackaged ? process.resourcesPath : join(__dirname, '..', '..', '..');
}

function backendDir(): string {
  return app.isPackaged
    ? join(resourcesDir(), 'backend')
    : join(resourcesDir(), 'services', 'converter', 'dist', 'converter');
}

function webDistDir(): string {
  return app.isPackaged
    ? join(resourcesDir(), 'web')
    : join(resourcesDir(), 'apps', 'web', 'dist');
}

async function createSupervisor(): Promise<BackendSupervisor> {
  const dataDir = appDataDir();
  const created = new BackendSupervisor({
    backendDir: backendDir(),
    appDataDir: dataDir,
    workDir: join(dataDir, 'work'),
    logDir: join(dataDir, 'logs'),
    webDistDir: webDistDir(),
    env: { AUTOMATION_EXECUTOR_ENABLED: 'true', DESKTOP_HOST_ENABLED: 'true' },
  });
  created.on('log', (line: string) => {
    // Logs stay in the main process; the renderer never sees the raw stream.
    process.stdout.write(`[backend] ${line}`);
  });
  return created;
}

async function startBackend(): Promise<RuntimeEndpoint> {
  supervisor = await createSupervisor();
  endpoint = await supervisor.start();
  grants = new LocalGrants({
    baseUrl: endpoint.baseUrl,
    bootstrapSecret: process.env.DESKTOP_BOOTSTRAP_SECRET ?? '',
  });
  return endpoint;
}

async function createWindow(): Promise<void> {
  const target = endpoint;
  if (!target) {
    dialog.showErrorBox(
      'Backend indisponível',
      'O backend não iniciou. Use “Tentar novamente” no diagnóstico do aplicativo.',
    );
    return;
  }

  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 960,
    minHeight: 640,
    title: APP_NAME,
    show: false,
    backgroundColor: '#0b0f14',
    webPreferences: {
      preload: join(__dirname, 'preload.js'),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      spellcheck: false,
    },
  });

  // Only the app's own origin may be loaded inside the privileged window.
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(target.baseUrl)) {
      event.preventDefault();
    }
  });
  mainWindow.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));

  mainWindow.once('ready-to-show', () => mainWindow?.show());
  mainWindow.on('close', (event) => {
    const preferences = profileStore?.read().preferences;
    if (!quitting && preferences?.close_to_tray) {
      event.preventDefault();
      mainWindow?.hide();
      ensureTray();
    }
  });

  await mainWindow.loadURL(`${target.baseUrl}/`);
}

function ensureTray(): void {
  if (tray) return;
  const icon = nativeImage.createEmpty();
  tray = new Tray(icon);
  tray.setToolTip(APP_NAME);
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: 'Abrir aplicativo', click: () => mainWindow?.show() },
      {
        label: 'Pausar novos agendamentos',
        click: () => {
          void pauseAutomation();
        },
      },
      {
        label: 'Sair',
        click: () => {
          quitting = true;
          app.quit();
        },
      },
    ]),
  );
  tray.on('double-click', () => mainWindow?.show());
}

async function pauseAutomation(): Promise<void> {
  const target = endpoint;
  if (!target) return;
  try {
    await fetch(`${target.baseUrl}/internal/v1/automation/pause`, {
      method: 'POST',
      headers: { 'x-internal-token': process.env.DESKTOP_BOOTSTRAP_SECRET ?? '' },
    });
  } catch {
    /* the tray action is best-effort; state stays visible in the UI */
  }
}

function registerIpc(): void {
  const guard = (event: unknown) => {
    const target = endpoint;
    assertTrustedSender(event as IpcEventLike, target?.baseUrl ?? 'http://127.0.0.1:');
  };

  ipcMain.handle(IPC_CHANNELS.pickSource, async (event, input: { kind?: unknown }) => {
    guard(event);
    const kind = validateSourcePickArgs(input?.kind);
    const result = await dialog.showOpenDialog({
      properties: kind === 'directory' ? ['openDirectory'] : ['openFile'],
      title: kind === 'directory' ? 'Escolher pasta da fonte' : 'Escolher arquivo da fonte',
    });
    if (result.canceled || result.filePaths.length === 0) {
      return null; // cancel never clears a valid setting
    }
    const selected = result.filePaths[0];
    assertSelectionShape(selected, kind === 'directory' ? 'models_directory' : 'file');
    // The renderer receives only the display path; the grant stays in main.
    return { display_path: selected, kind };
  });

  ipcMain.handle(IPC_CHANNELS.pickModelResource, async (event, input: { kind?: unknown }) => {
    guard(event);
    const kind = validateModelPickArgs(input?.kind);
    const result = await dialog.showOpenDialog({
      properties:
        kind === 'models_directory' ? ['openDirectory'] : ['openFile'],
      filters:
        kind === 'gguf'
          ? [{ name: 'Modelo GGUF', extensions: ['gguf'] }]
          : kind === 'ollama_executable'
            ? [{ name: 'Ollama', extensions: ['exe'] }]
            : undefined,
      title:
        kind === 'models_directory'
          ? 'Escolher pasta de modelos do Ollama'
          : kind === 'gguf'
            ? 'Escolher arquivo GGUF'
            : 'Escolher ollama.exe',
    });
    if (result.canceled || result.filePaths.length === 0) return null;
    const selected = result.filePaths[0];
    assertSelectionShape(selected, kind);
    return { kind, path: selected };
  });

  ipcMain.handle(IPC_CHANNELS.readProfile, async (event) => {
    guard(event);
    return profileStore?.redacted() ?? {};
  });

  ipcMain.handle(IPC_CHANNELS.testProfile, async (event, draft: unknown) => {
    guard(event);
    const target = endpoint;
    if (!target) throw new IpcError('BACKEND_UNAVAILABLE', 'Backend indisponível.');
    const response = await fetch(`${target.baseUrl}/internal/v1/models/diagnostics`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-internal-token': process.env.DESKTOP_BOOTSTRAP_SECRET ?? '',
      },
      body: JSON.stringify({ draft }),
    });
    return (await response.json()) as Record<string, unknown>;
  });

  ipcMain.handle(
    IPC_CHANNELS.applyProfile,
    async (event, draft: unknown, expectedVersion: unknown) => {
      guard(event);
      const parsed = validateApplyArgs(draft, expectedVersion);
      const merged = { ...(profileStore?.read() ?? {}), ...parsed.draft } as never;
      validateDraft(merged);
      const applied = profileStore?.write(merged, parsed.expectedVersion);
      // Restart only the process this app owns; the previous profile is kept
      // in backups/ by the backend migration step if it fails.
      supervisor = await createSupervisor();
      endpoint = await supervisor.start();
      return { status: 'applied', applied_version: applied?.version ?? null, blockers: [] };
    },
  );

  ipcMain.handle(IPC_CHANNELS.runtimeStatus, async (event) => {
    guard(event);
    const target = endpoint;
    return {
      backend: {
        running: Boolean(target),
        port: target?.port ?? null,
        version: target?.version ?? null,
        boot_id: target?.bootId ?? null,
      },
      automation_executor_enabled: true,
    };
  });
}

// Single instance: a second launch focuses the existing window.
const lock = app.requestSingleInstanceLock();
if (!lock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (!mainWindow.isVisible()) mainWindow.show();
      mainWindow.focus();
    }
  });

  app.whenReady().then(async () => {
    profileStore = new ProfileStore(appDataDir());
    registerIpc();
    try {
      await startBackend();
      await createWindow();
      ensureTray();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      dialog.showErrorBox('Falha ao iniciar', message);
      app.quit();
    }
  });

  app.on('before-quit', async (event) => {
    if (quitting || !supervisor) return;
    quitting = true;
    event.preventDefault();
    await supervisor.stop();
    app.quit();
  });

  app.on('window-all-closed', () => {
    // Keep running in the tray when configured; otherwise quit.
    const preferences = profileStore?.read().preferences;
    if (!preferences?.close_to_tray) {
      quitting = true;
      app.quit();
    }
  });
}
