/**
 * Electron main process (T12/T13, doc 03 §4/§5/§8).
 *
 * Single instance → identity and profile → backend supervisor → managed Ollama
 * (when configured) → window on the backend's own loopback origin (same origin,
 * relative /api/v1) → tray.
 *
 * Privileged local operations (root grants, model diagnostics, apply, GGUF
 * import) use the per-boot internal token AND the per-start desktop host
 * secret the supervisor shares with the backend. Both are injected here, never
 * handed to the renderer. The page reaches the backend with the same device id
 * the host uses, so granted roots and the page share one owner.
 */

import { app, BrowserWindow, dialog, ipcMain, Menu, nativeImage, session, Tray } from 'electron';
import { join } from 'node:path';

import { BackendSupervisor, type RuntimeEndpoint } from './backend-supervisor';
import {
  IPC_CHANNELS,
  IpcError,
  assertSelectionShape,
  assertTrustedSender,
  validateApplyArgs,
  validateApplyOptions,
  validateGgufTagArgs,
  validateModelPickArgs,
  validateSourcePickArgs,
  type IpcEventLike,
} from './ipc-handlers';
import { IdentityError, loadOrCreateDeviceId, loadOrCreateJwtSecret, resolveOwnerId } from './identity';
import { installHostSession } from './host-session';
import { LocalGrants, GrantError, parentDirectoryForGrant } from './local-grants';
import {
  ManagedOllama,
  ModelManagerError,
  planGgufImport,
  runGgufImport,
  tagDigest,
} from './model-manager';
import { applyProfileDraft, fetchRuntimeBlockers } from './profile-apply';
import { ProfileError, ProfileStore, type RuntimeProfile } from './runtime-profile';

const APP_NAME = 'Conversor Generator';
/** Query-free argument that carries the device id into the preload (sync). */
const DEVICE_ARG_PREFIX = '--conversor-device-id=';

app.setName(APP_NAME);

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let supervisor: BackendSupervisor | null = null;
let endpoint: RuntimeEndpoint | null = null;
let profileStore: ProfileStore | null = null;
let grants: LocalGrants | null = null;
let managedOllama: ManagedOllama | null = null;
let desktopDeviceId: string | null = null;
let importing = false;
/** Set when a quit was requested (tray, last window, deliberate shutdown). */
let quitting = false;
/** Shutdown (managed Ollama + backend) has finished; the next quit is final. */
let shutdownComplete = false;
let shutdownStarted = false;
/** True while a deliberate restart (profile apply, crash recovery) is in flight. */
let restarting = false;
/** Wall-clock start of the current backend run (for the crash budget). */
let lastStartedAt = 0;
/** One automatic restart per crash episode; a stable run earns it back. */
let automaticRestartAvailable = true;
const STABLE_RUN_MS = 60_000;
/** Delay before reloading the window after an apply, so the IPC reply is delivered first. */
const RELOAD_AFTER_REPLY_MS = 800;

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

function requireHostSecret(): string {
  const secret = supervisor?.bootstrapSecret;
  if (!secret) {
    throw new IpcError('BACKEND_UNAVAILABLE', 'Backend indisponível.');
  }
  return secret;
}

function newGrants(): LocalGrants {
  const current = supervisor;
  if (!current || !endpoint) {
    throw new IpcError('BACKEND_UNAVAILABLE', 'Backend indisponível.');
  }
  return new LocalGrants({
    baseUrl: endpoint.baseUrl,
    bootstrapSecret: current.internalToken,
    hostSecret: requireHostSecret(),
  });
}

async function startBackend(profile?: RuntimeProfile): Promise<RuntimeEndpoint> {
  const active = profile ?? profileStore?.read() ?? null;
  supervisor = new BackendSupervisor({
    backendDir: backendDir(),
    appDataDir: appDataDir(),
    workDir: join(appDataDir(), 'work'),
    logDir: join(appDataDir(), 'logs'),
    webDistDir: webDistDir(),
    jwtSecret: loadOrCreateJwtSecret(appDataDir()),
    env: active ? profileEnvironment(active) : undefined,
  });
  supervisor.on('log', (line: string) => process.stdout.write(`[backend] ${line}`));
  watchBackend(supervisor);
  endpoint = await supervisor.start();
  lastStartedAt = Date.now();
  grants = newGrants();
  return endpoint;
}

/**
 * An exit after READY must never be silent. Deliberate stops (app quit, profile
 * apply, crash recovery itself) are ignored here via `quitting` / `restarting`.
 */
function watchBackend(instance: BackendSupervisor): void {
  instance.on('stopped', (code: number | null) => {
    if (quitting || restarting || supervisor !== instance) return;
    void handleUnexpectedStop(code);
  });
}

async function handleUnexpectedStop(code: number | null): Promise<void> {
  endpoint = null;
  const reason = `código de saída ${code ?? 'desconhecido'}`;
  process.stderr.write(`[backend] encerrado inesperadamente (${reason})\n`);

  if (Date.now() - lastStartedAt > STABLE_RUN_MS) {
    automaticRestartAvailable = true;
  }
  if (automaticRestartAvailable) {
    automaticRestartAvailable = false;
    if (await tryRestartBackend()) return;
  }
  await offerBackendRecovery(reason);
}

/** Restarts the SAME supervisor with the applied profile; returns false on failure. */
async function tryRestartBackend(): Promise<boolean> {
  try {
    await restartBackend(profileStore ? profileEnvironment(profileStore.read()) : {});
  } catch (error) {
    process.stderr.write(
      `[backend] reinício falhou: ${error instanceof Error ? error.message : String(error)}\n`,
    );
    return false;
  }
  // The restarted backend listens on a new port: the page must load the new origin.
  reloadWindow();
  return true;
}

function reloadWindow(): void {
  if (mainWindow && !mainWindow.isDestroyed() && endpoint) {
    void mainWindow.loadURL(`${endpoint.baseUrl}/`);
  }
}

/** Reload after the current IPC reply has reached the page (F6). */
function scheduleWindowReload(): void {
  setTimeout(reloadWindow, RELOAD_AFTER_REPLY_MS);
}

async function offerBackendRecovery(reason: string): Promise<void> {
  for (;;) {
    const choice = await dialog.showMessageBox({
      type: 'error',
      title: APP_NAME,
      message: 'O backend do Conversor Generator parou inesperadamente.',
      detail:
        `Motivo: ${reason}.\n\n` +
        `Os registros do backend estão em:\n${join(appDataDir(), 'logs')}\n\n` +
        '“Reiniciar backend” tenta iniciá-lo novamente. “Sair” fecha o aplicativo.',
      buttons: ['Reiniciar backend', 'Sair'],
      defaultId: 0,
      cancelId: 1,
      noLink: true,
    });
    if (choice.response !== 0) {
      quitting = true;
      app.quit();
      return;
    }
    if (await tryRestartBackend()) return;
  }
}

/** Deliberate restart of the single supervisor; the old child's exit is not a crash. */
async function restartBackend(extraEnv: Record<string, string>): Promise<RuntimeEndpoint> {
  const current = supervisor;
  if (!current) {
    throw new IpcError('BACKEND_UNAVAILABLE', 'Backend indisponível.');
  }
  restarting = true;
  try {
    const next = await current.restart(extraEnv);
    endpoint = next;
    grants = newGrants();
    lastStartedAt = Date.now();
    return next;
  } finally {
    restarting = false;
  }
}

/** Restart the backend with a profile (starts it when it never ran). */
async function restartWithProfile(profile: RuntimeProfile): Promise<void> {
  if (supervisor) {
    await restartBackend(profileEnvironment(profile));
  } else {
    await startBackend(profile);
  }
}

/** Starts the managed Ollama for a managed profile; stops ours otherwise. */
async function startManagedRuntime(profile: RuntimeProfile): Promise<void> {
  await stopManagedRuntime();
  if (profile.mode !== 'managed' || !profile.ollama_path) return;
  const managed = new ManagedOllama({
    executable: profile.ollama_path,
    modelsDir: profile.models_dir,
    host: profile.ocr.endpoint_host,
    port: profile.ocr.endpoint_port,
  });
  await managed.start();
  managedOllama = managed;
}

/** Stops only the Ollama process this app started; a foreign one is never touched. */
async function stopManagedRuntime(): Promise<void> {
  const current = managedOllama;
  managedOllama = null;
  if (current) await current.stop();
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
      additionalArguments: desktopDeviceId ? [`${DEVICE_ARG_PREFIX}${desktopDeviceId}`] : [],
    },
  });

  // Only the app's own origin may be loaded inside the privileged window.
  mainWindow.webContents.on('will-navigate', (event, url) => {
    // Read the current endpoint: a crash recovery moves the backend to a new port.
    if (!endpoint || !url.startsWith(endpoint.baseUrl)) {
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

/** Headers for host-privileged calls: internal token plus the desktop host secret. */
function internalHeaders(): Record<string, string> {
  const token = supervisor?.internalToken;
  if (!token) {
    throw new IpcError('BACKEND_UNAVAILABLE', 'Backend indisponível.');
  }
  return {
    'x-internal-token': token,
    'x-desktop-host': requireHostSecret(),
    'content-type': 'application/json',
  };
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

/**
 * Owner id of the desktop session. The SAME device id that the preload gives the
 * page is used here, so both sides resolve to one owner, also after a restart
 * on another port (F4).
 */
async function desktopOwnerId(): Promise<string> {
  const target = endpoint;
  if (!target) throw new IpcError('BACKEND_UNAVAILABLE', 'Backend indisponível.');
  try {
    return await resolveOwnerId({
      baseUrl: target.baseUrl,
      deviceId: loadOrCreateDeviceId(appDataDir()),
      hostSecret: requireHostSecret(),
    });
  } catch (error) {
    if (error instanceof IdentityError) {
      throw new IpcError(error.code, error.message);
    }
    throw error;
  }
}

/** Runtime blockers for a drain check; no running backend means nothing to drain. */
async function runtimeBlockers(): Promise<string[]> {
  const target = endpoint;
  if (!target) return [];
  return fetchRuntimeBlockers({ baseUrl: target.baseUrl, headers: internalHeaders() });
}

/** Maps domain errors to the IPC error envelope the page understands. */
function toIpcError(error: unknown): unknown {
  if (
    error instanceof ProfileError ||
    error instanceof ModelManagerError ||
    error instanceof IdentityError ||
    error instanceof GrantError
  ) {
    return new IpcError(error.code, error.message);
  }
  return error;
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
    // A single file: grant its parent folder (drive roots keep their separator),
    // keep the file name relative.
    const filename = selected.split(/[\\/]/).pop() ?? '';
    const granted = await grants.grant({
      ownerId,
      canonicalPath: parentDirectoryForGrant(selected),
    });
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
    async (event, draft: unknown, expectedVersion: unknown, options: unknown) => {
      guard(event);
      const parsed = validateApplyArgs(draft, expectedVersion);
      const { force } = validateApplyOptions(options);
      const store = profileStore;
      if (!store) {
        throw new IpcError('PROFILE_UNAVAILABLE', 'Perfil local indisponível.');
      }
      try {
        return await applyProfileDraft(parsed.draft, parsed.expectedVersion, { force }, {
          store,
          blockers: runtimeBlockers,
          restart: restartWithProfile,
          startManaged: startManagedRuntime,
          scheduleReload: scheduleWindowReload,
          log: (message) => process.stderr.write(`[profile] ${message}\n`),
        });
      } catch (error) {
        throw toIpcError(error);
      }
    },
  );

  ipcMain.handle(IPC_CHANNELS.importGguf, async (event, input: { tag?: unknown }) => {
    guard(event);
    const tag = validateGgufTagArgs(input?.tag);
    const store = profileStore;
    const profile = store?.read();
    if (!profile || profile.mode !== 'managed' || !profile.ollama_path) {
      throw new IpcError(
        'GGUF_REQUIRES_MANAGED',
        'A importação de GGUF exige o modo “Iniciar Ollama pelo aplicativo” com ollama.exe configurado.',
      );
    }
    if (importing) {
      throw new IpcError('IMPORT_BUSY', 'Já existe uma importação em andamento.');
    }
    if (!managedOllama?.running) {
      throw new IpcError('OLLAMA_NOT_RUNNING', 'O Ollama gerenciado não está em execução.');
    }
    const picked = await dialog.showOpenDialog({
      properties: ['openFile'],
      filters: [{ name: 'Modelo GGUF', extensions: ['gguf'] }],
      title: 'Escolher arquivo GGUF',
    });
    if (picked.canceled || picked.filePaths.length === 0) return null;
    const selected = picked.filePaths[0];
    assertSelectionShape(selected, 'gguf');

    importing = true;
    let plan: ReturnType<typeof planGgufImport> | null = null;
    try {
      plan = planGgufImport(selected, tag);
      const host = profile.ocr.endpoint_host;
      const port = profile.ocr.endpoint_port;
      await runGgufImport({
        executable: profile.ollama_path,
        host,
        port,
        plan,
        onProgress: (line) => mainWindow?.webContents.send(IPC_CHANNELS.ggufProgress, line),
      });
      const digest = await tagDigest(`http://${host}:${port}`, tag);
      return { tag, digest, status: 'imported' };
    } catch (error) {
      throw toIpcError(error);
    } finally {
      plan?.cleanup();
      importing = false;
    }
  });

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
    // Header injection and CSP for the backend origin only; both read the
    // current origin and secret, so a restart on another port needs no re-registration.
    installHostSession(session.defaultSession.webRequest, {
      baseUrl: () => endpoint?.baseUrl ?? null,
      secret: () => supervisor?.bootstrapSecret ?? null,
    });
    try {
      desktopDeviceId = loadOrCreateDeviceId(appDataDir());
      await startBackend();
      try {
        await startManagedRuntime(profileStore.read());
      } catch (error) {
        // The backend still runs; the diagnostics show the runtime as unavailable.
        process.stderr.write(
          `[ollama] não foi possível iniciar o Ollama gerenciado: ${
            error instanceof Error ? error.message : String(error)
          }\n`,
        );
      }
      await createWindow();
      ensureTray();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      dialog.showErrorBox('Falha ao iniciar', message);
      app.quit();
    }
  });

  /**
   * Deliberate shutdown: stop the managed Ollama we started, then the backend
   * tree we spawned, and only then really quit. `quitting` alone is not enough:
   * the tray and the last window both set it before this handler runs.
   */
  app.on('before-quit', (event) => {
    quitting = true;
    if (shutdownComplete) return;
    event.preventDefault();
    if (shutdownStarted) return;
    shutdownStarted = true;
    void (async () => {
      try {
        await stopManagedRuntime();
        await supervisor?.stop();
      } finally {
        shutdownComplete = true;
        app.quit();
      }
    })();
  });

  app.on('window-all-closed', () => {
    const preferences = profileStore?.read().preferences;
    if (!preferences?.close_to_tray) {
      quitting = true;
      app.quit();
    }
  });
}
