/**
 * Restricted, typed bridge (T12, doc 03 §5).
 *
 * Only these methods exist. There is no `fs`, `exec`, `spawn`, no arbitrary
 * IPC channel and no "read any path" helper — the renderer cannot escalate.
 */

import { contextBridge, ipcRenderer } from 'electron';

export interface ModelSelection {
  kind: 'ollama_executable' | 'models_directory' | 'gguf';
  path: string;
}

export interface DesktopBridge {
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
  ): Promise<Record<string, unknown>>;
  getRuntimeStatus(): Promise<Record<string, unknown>>;
}

const bridge: DesktopBridge = {
  pickSource: (input) => ipcRenderer.invoke('desktop:pick-source', input),
  pickModelResource: (input) => ipcRenderer.invoke('desktop:pick-model-resource', input),
  readRuntimeProfile: () => ipcRenderer.invoke('desktop:read-profile'),
  testModelProfile: (draft) => ipcRenderer.invoke('desktop:test-profile', draft),
  applyRuntimeProfile: (draft, expectedVersion) =>
    ipcRenderer.invoke('desktop:apply-profile', draft, expectedVersion),
  getRuntimeStatus: () => ipcRenderer.invoke('desktop:runtime-status'),
};

contextBridge.exposeInMainWorld('desktop', bridge);
