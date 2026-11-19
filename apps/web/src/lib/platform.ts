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

export interface DesktopApplyOptions {
  /** Apply even while backend work is running (explicit operator choice). */
  force?: boolean;
}

export interface DesktopBridge {
  /**
   * Owner key of this installation, shared with the host's granted roots (F4).
   * Used as the page's device id instead of a browser-only value.
   */
  readonly deviceId?: string;
  pickSource(input: { kind: 'file' | 'directory' }): Promise<DesktopSelection | null>;
  pickModelResource(input: {
    kind: DesktopModelSelection['kind'];
  }): Promise<DesktopModelSelection | null>;
  readRuntimeProfile(): Promise<Record<string, unknown>>;
  testModelProfile(draft: Record<string, unknown>): Promise<Record<string, unknown>>;
  applyRuntimeProfile(
    draft: Record<string, unknown>,
    expectedVersion: number | null,
    options?: DesktopApplyOptions,
  ): Promise<Record<string, unknown>>;
  getRuntimeStatus(): Promise<Record<string, unknown>>;
  importGguf?(input: { tag: string }): Promise<Record<string, unknown> | null>;
  onGgufProgress?(listener: (line: string) => void): () => void;
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

/**
 * The host's device id when running in the desktop; `null` in a browser. The
 * value is used as the device identity INSTEAD of localStorage/cookies, so the
 * page and the host resolve to the same owner.
 */
export function desktopDeviceId(): string | null {
  const id = desktopBridge()?.deviceId;
  return typeof id === 'string' && id.length > 0 ? id : null;
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
