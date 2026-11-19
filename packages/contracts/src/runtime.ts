/**
 * Runtime profile contracts for the desktop host (upgrade plan, doc 03).
 * ============================================================================
 * The machine profile lives in a versioned JSON file private to the desktop
 * app (never in the shared database). The web UI renders a redacted view;
 * only the Electron main process reads/writes the real profile.
 */

/** How the desktop connects to a local Ollama runtime. */
export type OllamaMode = 'external' | 'managed';

export interface RuntimeProfile {
  /** Monotonic profile revision; optimistic concurrency for apply flows. */
  version: number;
  mode: OllamaMode;
  /** Path to `ollama.exe`; required in managed mode and for GGUF import. */
  ollama_path: string | null;
  /** Models repository applied as `OLLAMA_MODELS` to the managed process only. */
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
}

/** Web-safe view: secrets and absolute host paths removed. */
export interface RedactedProfile {
  version: number;
  mode: OllamaMode;
  ocr_enabled: boolean;
  ocr_model: string | null;
  assistant_enabled: boolean;
  assistant_model: string | null;
  /** True when a Ollama executable path is saved; the path itself is never sent. */
  ollama_path_configured: boolean;
  /** True when a models directory is saved; the path itself is never sent. */
  models_dir_configured: boolean;
}

export interface ModelProfileDraft {
  mode: OllamaMode;
  ollama_path: string | null;
  models_dir: string | null;
  ocr_enabled: boolean;
  ocr_model: string | null;
  assistant_enabled: boolean;
  assistant_model: string | null;
  context_tokens: number;
  max_output_tokens: number;
}

export interface ModelDiagnostics {
  runtime_detected: boolean;
  runtime_version: string | null;
  ocr_model_available: boolean;
  assistant_model_available: boolean;
  ocr_latency_ms: number | null;
  assistant_latency_ms: number | null;
  ocr_error: string | null;
  assistant_error: string | null;
  /** e.g. memory/timeout failures observed while probing. */
  notes: string[];
}

export type ApplyProfileResultStatus = 'applied' | 'deferred' | 'rolled_back';

export interface ApplyProfileResult {
  status: ApplyProfileResultStatus;
  /** Non-terminal work that prevented immediate application (human-readable). */
  blockers: string[];
  applied_version: number | null;
  /** Set when status is `rolled_back`: why the new profile was reverted. */
  error?: string | null;
}

/** Non-terminal work counted by the backend (`GET /internal/v1/runtime/activity`). */
export interface RuntimeActivity {
  queued: number;
  running: number;
  waiting_children: number;
  automation_running: number;
  active_imports: number;
  indexing: number;
  total: number;
}

export interface RuntimeStatus {
  backend: {
    running: boolean;
    port: number | null;
    version: string | null;
    boot_id: string | null;
  };
  ollama: {
    mode: OllamaMode;
    reachable: boolean;
    version: string | null;
    models: string[];
  };
  automation_executor_enabled: boolean;
}

/** Resource kinds the desktop picker can select for model configuration. */
export type ModelResourceKind = 'ollama_executable' | 'models_directory' | 'gguf';

export interface ModelSelection {
  kind: ModelResourceKind;
  path: string;
}
