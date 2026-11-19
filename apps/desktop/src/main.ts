/**
 * Electron main process (T12/T13, doc 03 §4/§5/§8).
 *
 * Single instance → profile → backend supervisor → window on the backend's
 * own loopback origin (same origin, relative /api/v1) → tray.
 *
 * Privileged local operations (root grants, model diagnostics, apply) use the
 * per-boot internal token the supervisor shares with the backend; the
 * renderer never receives that token.
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
import { ProfileStore, validateDraft, type RuntimeProfile } from './runtime-profile';

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

/** Profile-derived environment for the backend (models, OCR, assistant). */
function profileEnvironment(profile: RuntimeProfile): Record<string, string> {
  return {
    AUTOMATION_EXECUTOR_ENABLED: 'true',
    DESKTOP_HOST_ENABLED: 'true',
    OLLAMA_MODE: profile.mode,
    GLMOCR_ENABLED: String(profile.ocr.enabled),
    GLMOCR_HOST: profile.ocr.endpoint_host,
    GLMOCR_PORT: String(profile.ocr.endpoint_port),
    GLMOCR_MODEL: profile.ocr.model ?? '',
    ASSISTANT_ENABLED: String(profile.assistant.enabled),
    ASSISTANT_HOST: profile.assistant.endpoint_host,
    ASSISTANT_PORT: String(profile.assistant.endpoint_port),
    ASSISTANT_MODEL: profile.assistant.model ?? '',
    ASSISTANT_CONTEXT_TOKENS: String(profile.assistant.context_tokens),
    ASSISTANT_MAX_OUTPUT_TOKENS: String(profile.assistant.max_output_tokens),
  };
}

async function startBackend(profile?: RuntimeProfile): Promise<RuntimeEndpoint> {
  const active = profile ?? profileStore?.read() ?? null;
  supervisor = new BackendSupervisor({
    backendDir: backendDir(),
    appDataDir: appDataDir(),
    workDir: join(appDataDir(), 'work'),
    logDir: join(appDataDir(), 'logs'),
    webDistDir: webDistDir(),
    env: active ? profileEnvironment(active) : undefined,
  });
  supervisor.on('log', (line: string) => process.stdout.write(`[backend] ${line}`));
  endpoint = await supervisor.start();
  grants = new LocalGrants({
    baseUrl: endpoint.baseUrl,
    bootstrapSecret: supervisor.internalToken,
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

function internalHeaders(): Record<string, string> {
  const token = supervisor?.internalToken;
  if (!token) {
    throw new IpcError('BACKEND_UNAVAILABLE', 'Backend indisponível.');
  }
  return { 'x-internal-token': token, 'content-type': 'application/json' };
}

async function pauseAutomation(): Promise<void> {
  const target = endpoint;
  if (!target) return;
  try {
    await fetch(`${target.baseUrl}/internal/v1/automation/pause`, {
      method: 'POST',
      headers: internalHeaders(),
    });
  } catch {
    /* the tray action is best-effort; state stays visible in the UI */
  }
}

/** Owner id of the desktop session (created by the backend on first call). */
async function desktopOwnerId(): Promise<string> {
  const target = endpoint;
  if (!target) throw new IpcError('BACKEND_UNAVAILABLE', 'Backend indisponível.');
  const identityFile = join(appDataDir(), 'identity', 'device-id');
  let deviceId: string;
  try {
    // Persisted outside localStorage so a backend restart on another port
    // keeps the same history (doc 03 §4 item 6).
    const fs = await import('node:fs');
    if (fs.existsSync(identityFile)) {
      deviceId = fs.readFileSync(identityFile, 'utf8').trim();
    } else {
      deviceId = BackendSupervisor.newToken();
      fs.mkdirSync(join(appDataDir(), 'identity'), { recursive: true });
      fs.writeFileSync(identityFile, deviceId, { encoding: 'utf8', mode: 0o600 });
    }
  } catch {
    deviceId = BackendSupervisor.newToken();
  }
  const response = await fetch(`${target.baseUrl}/api/v1/auth/anonymous`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'x-device-id': deviceId },
    body: '{}',
  });
  if (!response.ok) {
    throw new IpcError('IDENTITY_UNAVAILABLE', 'Não foi possível estabelecer a identidade local.');
  }
  const session = (await response.json()) as { user?: { id?: string } };
  const ownerId = session.user?.id;
  if (!ownerId) {
    throw new IpcError('IDENTITY_UNAVAILABLE', 'Sessão local sem identificador.');
  }
  return ownerId;
}

function registerIpc(): void {
  const guard = (event: unknown) => {
    const target = endpoint;
    assertTrustedSender(event as IpcEventLike, target?.baseUrl ?? 'http://127.0.0.1:');
  };

  /**
   * The native picker DOES grant the chosen folder: the root is created with
   * the host credential and only a relative reference goes back to the page.
   */
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

    const ownerId = await desktopOwnerId();
    if (!grants) {
      throw new IpcError('GRANT_UNAVAILABLE', 'Concessão local indisponível.');
    }
    if (kind === 'directory') {
      const granted = await grants.grant({ ownerId, canonicalPath: selected });
      // The page works relative to the granted root: '.' means the root itself.
      return { display_path: '.', kind, root_id: granted.rootId };
    }
    // A single file: grant its parent folder, keep the file name relative.
    const parts = selected.split(/[\\/]/);
    const filename = parts.pop() ?? '';
    const parent = parts.join(process.platform === 'win32' ? '\\' : '/');
    const granted = await grants.grant({ ownerId, canonicalPath: parent });
    return { display_path: filename, kind, root_id: granted.rootId };
  });

  ipcMain.handle(IPC_CHANNELS.pickModelResource, async (event, input: { kind?: unknown }) => {
    guard(event);
    const kind = validateModelPickArgs(input?.kind);
    const result = await dialog.showOpenDialog({
      properties: kind === 'models_directory' ? ['openDirectory'] : ['openFile'],
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
      headers: internalHeaders(),
      body: JSON.stringify({ draft }),
    });
    return (await response.json()) as Record<string, unknown>;
  });

  ipcMain.handle(
    IPC_CHANNELS.applyProfile,
    async (event, draft: unknown, expectedVersion: unknown) => {
      guard(event);
      const parsed = validateApplyArgs(draft, expectedVersion);
      const store = profileStore;
      if (!store) {
        throw new IpcError('PROFILE_UNAVAILABLE', 'Perfil local indisponível.');
      }
      const merged = { ...store.read(), ...parsed.draft } as RuntimeProfile;
      validateDraft(merged);
      const applied = store.write(merged, parsed.expectedVersion);
      if (!applied) {
        throw new IpcError('PROFILE_UNAVAILABLE', 'Perfil local indisponível.');
      }
      // Restart the SAME supervisor (stops the previous child) with the new
      // profile, so no duplicate backend runs against the same database.
      const previous = store.read();
      try {
        const restarted = supervisor ? await supervisor.restart(profileEnvironment(applied)) : await startBackend(applied);
        endpoint = restarted;
        return { status: 'applied', applied_version: applied.version, blockers: [] };
      } catch (error) {
        // Roll back to the previous version so a failed apply is not sticky.
        store.write(previous, applied.version);
        throw new IpcError(
          'PROFILE_APPLY_FAILED',
          `A aplicação falhou e a versão anterior foi restaurada: ${
            error instanceof Error ? error.message : String(error)
          }`,
        );
      }
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
    const preferences = profileStore?.read().preferences;
    if (!preferences?.close_to_tray) {
      quitting = true;
      app.quit();
    }
  });
}
