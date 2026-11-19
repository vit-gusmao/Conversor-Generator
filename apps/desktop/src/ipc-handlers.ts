/**
 * IPC handlers with strict validation (T12).
 *
 * Every channel validates the sender frame (only the app's own renderer may
 * call) and the argument shape. No channel exposes `fs`, `exec`, `spawn` or a
 * generic "read any path": the renderer gets typed results only.
 */

import { existsSync, statSync } from 'node:fs';

export interface IpcEventLike {
  senderFrame?: { url?: string } | null;
}

export class IpcError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = 'IpcError';
    this.code = code;
  }
}

export const IPC_CHANNELS = {
  pickSource: 'desktop:pick-source',
  pickModelResource: 'desktop:pick-model-resource',
  readProfile: 'desktop:read-profile',
  testProfile: 'desktop:test-profile',
  applyProfile: 'desktop:apply-profile',
  runtimeStatus: 'desktop:runtime-status',
} as const;

const SOURCE_KINDS = new Set(['file', 'directory']);
const MODEL_KINDS = new Set(['ollama_executable', 'models_directory', 'gguf']);

/**
 * The sender must be our own SPA. A foreign page (file://, http://evil) that
 * somehow reached the window is refused before any work happens.
 */
export function assertTrustedSender(event: IpcEventLike, allowedOrigin: string): void {
  const url = event?.senderFrame?.url ?? '';
  if (!url) {
    throw new IpcError('IPC_FORBIDDEN', 'Origem do chamador ausente.');
  }
  if (!url.startsWith(allowedOrigin)) {
    throw new IpcError(
      'IPC_FORBIDDEN',
      'Somente a interface carregada pelo próprio aplicativo pode usar privilégios locais.',
    );
  }
}

export function validateSourcePickArgs(kind: unknown): 'file' | 'directory' {
  if (typeof kind !== 'string' || !SOURCE_KINDS.has(kind)) {
    throw new IpcError('IPC_INVALID_ARGUMENT', "kind deve ser 'file' ou 'directory'.");
  }
  return kind as 'file' | 'directory';
}

export function validateModelPickArgs(kind: unknown): 'ollama_executable' | 'models_directory' | 'gguf' {
  if (typeof kind !== 'string' || !MODEL_KINDS.has(kind)) {
    throw new IpcError(
      'IPC_INVALID_ARGUMENT',
      "kind deve ser 'ollama_executable', 'models_directory' ou 'gguf'.",
    );
  }
  return kind as 'ollama_executable' | 'models_directory' | 'gguf';
}

export function validateApplyArgs(
  draft: unknown,
  expectedVersion: unknown,
): { draft: Record<string, unknown>; expectedVersion: number | null } {
  if (typeof draft !== 'object' || draft === null || Array.isArray(draft)) {
    throw new IpcError('IPC_INVALID_ARGUMENT', 'O perfil candidato deve ser um objeto.');
  }
  if (expectedVersion !== null && (!Number.isInteger(expectedVersion) || (expectedVersion as number) < 0)) {
    throw new IpcError('IPC_INVALID_ARGUMENT', 'expectedVersion deve ser inteiro.');
  }
  return { draft: draft as Record<string, unknown>, expectedVersion: expectedVersion as number | null };
}

/** Selected paths must exist and match the requested resource kind. */
export function assertSelectionShape(path: unknown, kind: string): string {
  if (typeof path !== 'string' || path.length === 0 || path.length > 1024) {
    throw new IpcError('IPC_INVALID_ARGUMENT', 'Caminho selecionado inválido.');
  }
  if (!existsSync(path)) {
    throw new IpcError('SELECTION_MISSING', 'O caminho selecionado não existe mais.');
  }
  const stats = statSync(path);
  if (kind === 'directory' || kind === 'models_directory') {
    if (!stats.isDirectory()) {
      throw new IpcError('SELECTION_WRONG_TYPE', 'Selecione uma pasta.');
    }
  } else if (!stats.isFile()) {
    throw new IpcError('SELECTION_WRONG_TYPE', 'Selecione um arquivo.');
  }
  if (kind === 'gguf' && !path.toLowerCase().endsWith('.gguf')) {
    throw new IpcError('SELECTION_WRONG_TYPE', 'Selecione um arquivo .gguf.');
  }
  if (kind === 'ollama_executable' && !/(ollama(\.exe)?)$/i.test(path)) {
    throw new IpcError('SELECTION_WRONG_TYPE', 'Selecione o executável ollama (ollama.exe).');
  }
  return path;
}

/** Cancelling a dialog returns null and never clears a valid setting. */
export function cancelledSelection(): null {
  return null;
}
