/**
 * Restricted, typed bridge (T12, doc 03 §5).
 *
 * Only these members exist. There is no `fs`, `exec`, `spawn`, no arbitrary
 * IPC channel and no "read any path" helper — the renderer cannot escalate.
 *
 * The sandboxed preload cannot import the main-side modules (they use `node:fs`),
 * so channel names are repeated here; `ipc-handlers.ts` is the source of truth.
 */

import { contextBridge, ipcRenderer } from 'electron';

export interface ModelSelection {
  kind: 'ollama_executable' | 'models_directory' | 'gguf';
  path: string;
}

export interface ApplyOptions {
  /** Apply even when backend work is in progress (operator's explicit choice). */
  force?: boolean;
}

export interface DesktopBridge {
  /**
   * Owner key of this installation. The page uses it as its device id, so the
   * granted roots and the page share one owner (F4).
   */
  readonly deviceId: string;
  pickSource(input: { kind: 'file' | 'directory' }): Promise<{
    display_path: string;
    kind: 'file' | 'directory';
    /** Root granted by the host; the page creates the source relative to it. */
    root_id: string;
  } | null>;
  pickModelResource(input: { kind: ModelSelection['kind'] }): Promise<ModelSelection | null>;
  readRuntimeProfile(): Promise<Record<string, unknown>>;
  testModelProfile(draft: Record<string, unknown>): Promise<Record<string, unknown>>;
  applyRuntimeProfile(
    draft: Record<string, unknown>,
    expectedVersion: number | null,
    options?: ApplyOptions,
  ): Promise<Record<string, unknown>>;
  getRuntimeStatus(): Promise<Record<string, unknown>>;
  /** Imports a .gguf chosen in a native dialog into the managed Ollama (managed mode only). */
  importGguf(input: { tag: string }): Promise<Record<string, unknown> | null>;
  /** Subscribes to import progress lines; returns the unsubscribe function. */
  onGgufProgress(listener: (line: string) => void): () => void;
}

const DEVICE_ARG_PREFIX = '--conversor-device-id=';

function deviceIdFromArguments(): string {
  const argument = (process.argv ?? []).find((item) => item.startsWith(DEVICE_ARG_PREFIX));
  return argument ? argument.slice(DEVICE_ARG_PREFIX.length) : '';
}

const bridge: DesktopBridge = {
  deviceId: deviceIdFromArguments(),
  pickSource: (input) => ipcRenderer.invoke('desktop:pick-source', input),
  pickModelResource: (input) => ipcRenderer.invoke('desktop:pick-model-resource', input),
  readRuntimeProfile: () => ipcRenderer.invoke('desktop:read-profile'),
  testModelProfile: (draft) => ipcRenderer.invoke('desktop:test-profile', draft),
  applyRuntimeProfile: (draft, expectedVersion, options) =>
    ipcRenderer.invoke('desktop:apply-profile', draft, expectedVersion, options ?? {}),
  getRuntimeStatus: () => ipcRenderer.invoke('desktop:runtime-status'),
  importGguf: (input) => ipcRenderer.invoke('desktop:import-gguf', input),
  onGgufProgress: (listener) => {
    const handler = (_event: unknown, line: unknown) => {
      if (typeof line === 'string') listener(line);
    };
    ipcRenderer.on('desktop:gguf-progress', handler);
    return () => {
      ipcRenderer.removeListener('desktop:gguf-progress', handler);
    };
  },
};

contextBridge.exposeInMainWorld('desktop', bridge);
