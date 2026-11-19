/**
 * Profile apply with drain and rollback (plan section 6, step 9).
 *
 * 1. the draft is merged null-safely over the stored profile and validated;
 * 2. unless forced, the backend is asked for non-terminal work; any work defers
 *    the apply WITHOUT touching the profile or the process;
 * 3. the profile is written atomically (optimistic version check);
 * 4. the backend restarts with the new environment;
 * 5. on any failure the previous profile bytes are restored and the previous
 *    environment is started again, and the result says `rolled_back`.
 *
 * Dependencies are injected so the whole sequence is testable without Electron.
 */

import { ProfileError, mergeDraft, type ProfileStore, type RuntimeProfile, validateDraft } from './runtime-profile';

export type ApplyStatus = 'applied' | 'deferred' | 'rolled_back';

export interface ApplyOutcome {
  status: ApplyStatus;
  applied_version: number | null;
  blockers: string[];
  error: string | null;
}

export interface ApplyDeps {
  store: ProfileStore;
  /** Non-terminal work in the running backend, already formatted for people. */
  blockers: () => Promise<string[]>;
  /** Restart the backend with this profile's environment. Rejects on failure. */
  restart: (profile: RuntimeProfile) => Promise<void>;
  /** Start or stop the managed Ollama for this profile. Rejects on failure. */
  startManaged: (profile: RuntimeProfile) => Promise<void>;
  /** Reload the window on the (possibly new) backend origin, after the reply is delivered. */
  scheduleReload: () => void;
  log?: (message: string) => void;
}

export const ACTIVITY_LABELS: Record<string, string> = {
  queued: 'na fila',
  running: 'em execução',
  waiting_children: 'aguardando subtarefas',
  automation_running: 'agendamentos em execução',
  active_imports: 'importações ativas',
  indexing: 'indexação de conhecimento',
};

/** Human-readable blockers from the backend's activity counters. */
export function summarizeActivity(counts: Record<string, unknown>): string[] {
  return Object.entries(counts)
    .filter(([key]) => key !== 'total')
    .map(([key, value]) => [key, Number(value)] as const)
    .filter(([, value]) => Number.isFinite(value) && value > 0)
    .map(([key, value]) => `${ACTIVITY_LABELS[key] ?? key}: ${value}`);
}

export interface ActivityRequest {
  baseUrl: string;
  headers: Record<string, string>;
  fetchImpl?: typeof fetch;
}

/**
 * Asks the running backend for its activity. A failed or malformed answer is a
 * blocker in its own right: an unknown state must not be applied silently.
 */
export async function fetchRuntimeBlockers(request: ActivityRequest): Promise<string[]> {
  const fetchImpl = request.fetchImpl ?? fetch;
  try {
    const response = await fetchImpl(`${request.baseUrl}/internal/v1/runtime/activity`, {
      headers: request.headers,
    });
    if (!response.ok) return ['estado de atividade indisponível'];
    const body = (await response.json()) as unknown;
    if (typeof body !== 'object' || body === null) return ['estado de atividade indisponível'];
    return summarizeActivity(body as Record<string, unknown>);
  } catch {
    return ['estado de atividade indisponível'];
  }
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * Throws ProfileError for invalid drafts and version conflicts (the caller maps
 * them to IPC errors). Every other failure is reported as `rolled_back`.
 */
export async function applyProfileDraft(
  draft: Record<string, unknown>,
  expectedVersion: number | null,
  options: { force: boolean },
  deps: ApplyDeps,
): Promise<ApplyOutcome> {
  const log = deps.log ?? (() => undefined);
  const current = deps.store.read();
  const merged = mergeDraft(current, draft);
  validateDraft(merged);

  if (!options.force) {
    const blockers = await deps.blockers();
    if (blockers.length > 0) {
      log(`aplicação adiada: ${blockers.join(', ')}`);
      return { status: 'deferred', applied_version: null, blockers, error: null };
    }
  }

  const snapshot = deps.store.snapshotRaw();
  // Version conflict throws ProfileError here, before anything changes.
  const applied = deps.store.write(merged, expectedVersion);

  try {
    await deps.restart(applied);
    await deps.startManaged(applied);
    deps.scheduleReload();
    return { status: 'applied', applied_version: applied.version, blockers: [], error: null };
  } catch (error) {
    const message = describe(error);
    log(`aplicação revertida: ${message}`);
    deps.store.restoreRaw(snapshot);
    const previous = deps.store.read();
    try {
      await deps.restart(previous);
      await deps.startManaged(previous);
      deps.scheduleReload();
    } catch (restoreError) {
      log(`reinício com a versão anterior também falhou: ${describe(restoreError)}`);
    }
    return { status: 'rolled_back', applied_version: null, blockers: [], error: message };
  }
}

export { ProfileError };
