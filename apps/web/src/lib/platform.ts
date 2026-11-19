/**
 * Desktop capability detection (T12).
 *
 * The web bundle must never import Electron: it only reads the typed bridge
 * the preload exposes. Without the bridge every feature degrades to the
 * browser behaviour (typing relative paths, no native picker).
 */

export interface DesktopSelection {
  display_path: string;
  kind: 'file' | 'directory';
  /** Root granted at pick time; create the source relative to it. */
  root_id: string;
}

export interface DesktopModelSelection {
  kind: 'ollama_executable' | 'models_directory' | 'gguf';
  path: string;
}

export interface DesktopBridge {
  pickSource(input: { kind: 'file' | 'directory' }): Promise<DesktopSelection | null>;
  pickModelResource(input: {
    kind: DesktopModelSelection['kind'];
  }): Promise<DesktopModelSelection | null>;
  readRuntimeProfile(): Promise<Record<string, unknown>>;
  testModelProfile(draft: Record<string, unknown>): Promise<Record<string, unknown>>;
  applyRuntimeProfile(
    draft: Record<string, unknown>,
    expectedVersion: number | null,
  ): Promise<Record<string, unknown>>;
  getRuntimeStatus(): Promise<Record<string, unknown>>;
}

declare global {
  interface Window {
    desktop?: DesktopBridge;
  }
}

export function hasDesktopBridge(): boolean {
  return typeof window !== 'undefined' && typeof window.desktop === 'object' && window.desktop !== null;
}

export function desktopBridge(): DesktopBridge | null {
  return hasDesktopBridge() ? (window.desktop as DesktopBridge) : null;
}

export function isDesktopRuntime(): boolean {
  if (!hasDesktopBridge()) return false;
  // The bridge only exists inside the Electron window.
  return true;
}

/** Native picker (grants the root) or `null` when cancelled/unavailable. */
export async function pickSource(kind: 'file' | 'directory'): Promise<DesktopSelection | null> {
  const bridge = desktopBridge();
  if (!bridge) return null;
  return bridge.pickSource({ kind });
}

export async function pickModelResource(
  kind: DesktopModelSelection['kind'],
): Promise<DesktopModelSelection | null> {
  const bridge = desktopBridge();
  if (!bridge) return null;
  return bridge.pickModelResource({ kind });
}
