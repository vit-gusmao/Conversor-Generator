/**
 * Versioned, atomic machine profile (T12/T13, doc 03 §3/§6).
 *
 * The profile is private to the desktop app: it never lives in the shared
 * database and it is written atomically (temp file + rename) so a crash
 * cannot leave a half-written profile behind. Applying a profile is
 * optimistic: the caller passes the version it read, and a mismatch is
 * rejected instead of silently overwriting.
 */

import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
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
      models_dir_configured: Boolean(profile.models_dir),
    };
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
