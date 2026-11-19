/**
 * Versioned, atomic machine profile (T12/T13, doc 03 §3/§6).
 *
 * The profile is private to the desktop app: it never lives in the shared
 * database and it is written atomically (temp file + rename) so a crash
 * cannot leave a half-written profile behind. Applying a profile is
 * optimistic: the caller passes the version it read, and a mismatch is
 * rejected instead of silently overwriting.
 */

import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

export const PROFILE_VERSION = 1;

export type OllamaMode = 'external' | 'managed';

export interface RuntimeProfile {
  version: number;
  mode: OllamaMode;
  ollama_path: string | null;
  models_dir: string | null;
  ocr: {
    enabled: boolean;
    model: string | null;
    endpoint_host: string;
    endpoint_port: number;
  };
  assistant: {
    enabled: boolean;
    model: string | null;
    endpoint_host: string;
    endpoint_port: number;
    context_tokens: number;
    max_output_tokens: number;
  };
  data_dir: string | null;
  work_dir: string | null;
  /** Preferences remembered from user choices (close-to-tray, startup). */
  preferences: {
    close_to_tray: boolean;
    launch_at_login: boolean;
  };
}

export interface RedactedProfile {
  version: number;
  mode: OllamaMode;
  ocr_enabled: boolean;
  ocr_model: string | null;
  assistant_enabled: boolean;
  assistant_model: string | null;
  ollama_path_configured: boolean;
  models_dir_configured: boolean;
}

export class ProfileError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = 'ProfileError';
    this.code = code;
  }
}

export function defaultProfile(dataDir: string | null = null, workDir: string | null = null): RuntimeProfile {
  return {
    version: 1,
    mode: 'external',
    ollama_path: null,
    models_dir: null,
    ocr: {
      enabled: true,
      model: 'glm-ocr:latest',
      endpoint_host: '127.0.0.1',
      endpoint_port: 11434,
    },
    assistant: {
      enabled: false,
      model: 'qwen3.5:4b',
      endpoint_host: '127.0.0.1',
      endpoint_port: 11434,
      context_tokens: 8192,
      max_output_tokens: 1024,
    },
    data_dir: dataDir,
    work_dir: workDir,
    preferences: { close_to_tray: true, launch_at_login: false },
  };
}

export class ProfileStore {
  private readonly filePath: string;

  constructor(userDataDir: string) {
    this.filePath = join(userDataDir, 'profile.json');
  }

  get path(): string {
    return this.filePath;
  }

  read(): RuntimeProfile {
    if (!existsSync(this.filePath)) {
      return defaultProfile();
    }
    try {
      const raw = JSON.parse(readFileSync(this.filePath, 'utf8')) as Partial<RuntimeProfile>;
      return mergeProfile(defaultProfile(), raw);
    } catch (error) {
      // A corrupt profile must not brick the app: fall back to defaults and
      // keep the broken file for diagnosis.
      renameSync(this.filePath, `${this.filePath}.corrupt-${Date.now()}`);
      return defaultProfile();
    }
  }

  redacted(): RedactedProfile {
    const profile = this.read();
    return {
      version: profile.version,
      mode: profile.mode,
      ocr_enabled: profile.ocr.enabled,
      ocr_model: profile.ocr.model,
      assistant_enabled: profile.assistant.enabled,
      assistant_model: profile.assistant.model,
      ollama_path_configured: Boolean(profile.ollama_path),
      models_dir_configured: Boolean(profile.models_dir),
    };
  }

  /** Raw bytes of the stored profile (null when none), for an exact rollback. */
  snapshotRaw(): string | null {
    return existsSync(this.filePath) ? readFileSync(this.filePath, 'utf8') : null;
  }

  /** Restores a snapshot atomically; `null` removes the file (back to defaults). */
  restoreRaw(raw: string | null): void {
    if (raw === null) {
      rmSync(this.filePath, { force: true });
      return;
    }
    mkdirSync(dirname(this.filePath), { recursive: true });
    const staging = `${this.filePath}.tmp-${process.pid}`;
    writeFileSync(staging, raw, { encoding: 'utf8', mode: 0o600 });
    renameSync(staging, this.filePath);
  }

  /**
   * Atomic write with optimistic concurrency. `expectedVersion === null`
   * means "create or replace unconditionally" (tests / first write).
   */
  write(next: RuntimeProfile, expectedVersion: number | null): RuntimeProfile {
    const current = this.read();
    if (expectedVersion !== null && current.version !== expectedVersion) {
      throw new ProfileError(
        'PROFILE_VERSION_CONFLICT',
        `Revisão do perfil divergente (esperado ${expectedVersion}, atual ${current.version}).`,
      );
    }
    const stamped: RuntimeProfile = {
      ...next,
      version: current.version + 1,
    };
    mkdirSync(dirname(this.filePath), { recursive: true });
    const staging = `${this.filePath}.tmp-${process.pid}`;
    writeFileSync(staging, JSON.stringify(stamped, null, 2), { encoding: 'utf8', mode: 0o600 });
    renameSync(staging, this.filePath);
    return stamped;
  }
}

export function mergeProfile(base: RuntimeProfile, patch: Partial<RuntimeProfile>): RuntimeProfile {
  return {
    ...base,
    ...patch,
    ocr: { ...base.ocr, ...(patch.ocr ?? {}) },
    assistant: { ...base.assistant, ...(patch.assistant ?? {}) },
    preferences: { ...base.preferences, ...(patch.preferences ?? {}) },
    // The stored revision survives a read (it is the optimistic token).
    version: patch.version ?? base.version,
  };
}

/** Keys a draft may set; anything else (version, data_dir, work_dir) is host-owned. */
const NESTED_KEYS = ['ocr', 'assistant', 'preferences'] as const;
const PATH_KEYS = ['ollama_path', 'models_dir'] as const;

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * Null-safe merge of a renderer draft over the stored profile (F5).
 *
 * - `undefined` and `null` never clear anything: a draft that omits a path, or
 *   sends it as null, keeps the saved path;
 * - a path is cleared only by an empty string or an explicit
 *   `clear_ollama_path` / `clear_models_dir` flag set to `true`;
 * - nested sections merge field by field, ignoring undefined fields.
 */
export function mergeDraft(current: RuntimeProfile, draft: Record<string, unknown>): RuntimeProfile {
  const next: RuntimeProfile = { ...current, ocr: { ...current.ocr }, assistant: { ...current.assistant }, preferences: { ...current.preferences } };

  if (draft.mode !== undefined && draft.mode !== null) {
    if (draft.mode !== 'external' && draft.mode !== 'managed') {
      throw new ProfileError('INVALID_MODE', "O modo deve ser 'external' ou 'managed'.");
    }
    next.mode = draft.mode as OllamaMode;
  }

  for (const key of PATH_KEYS) {
    const value = draft[key];
    if (value === undefined || value === null) continue;
    if (typeof value !== 'string') {
      throw new ProfileError('INVALID_PATH', `${key} deve ser texto.`);
    }
    next[key] = value === '' ? null : value;
  }
  if (draft.clear_ollama_path === true) next.ollama_path = null;
  if (draft.clear_models_dir === true) next.models_dir = null;

  for (const key of NESTED_KEYS) {
    const section = draft[key];
    if (!isPlainObject(section)) continue;
    const target = next[key] as unknown as Record<string, unknown>;
    for (const [field, value] of Object.entries(section)) {
      if (value === undefined) continue;
      if (!(field in target)) continue; // unknown fields never reach the profile
      target[field] = value;
    }
  }
  return next;
}

/** Only loopback endpoints are accepted in the first desktop version. */
export function assertLoopbackEndpoint(host: string): void {
  const normalized = (host ?? '').trim().toLowerCase();
  if (!['127.0.0.1', 'localhost', '::1'].includes(normalized)) {
    throw new ProfileError(
      'ENDPOINT_NOT_LOOPBACK',
      'Nesta versão o modelo local deve ser acessado por loopback; modelos remotos exigem uma opção explícita futura.',
    );
  }
}

export function validateDraft(draft: RuntimeProfile): void {
  assertLoopbackEndpoint(draft.ocr.endpoint_host);
  assertLoopbackEndpoint(draft.assistant.endpoint_host);
  if (draft.mode === 'managed' && !draft.ollama_path) {
    throw new ProfileError(
      'OLLAMA_PATH_REQUIRED',
      'No modo gerenciado, informe o caminho do ollama.exe.',
    );
  }
  if (!Number.isInteger(draft.ocr.endpoint_port) || draft.ocr.endpoint_port <= 0) {
    throw new ProfileError('INVALID_PORT', 'Porta do endpoint OCR inválida.');
  }
}
