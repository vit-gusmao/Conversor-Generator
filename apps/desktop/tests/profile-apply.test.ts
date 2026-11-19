import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { describe, expect, it, vi } from 'vitest';

import {
  applyProfileDraft,
  fetchRuntimeBlockers,
  summarizeActivity,
  type ApplyDeps,
} from '../src/profile-apply';
import { ProfileError, ProfileStore, defaultProfile, type RuntimeProfile } from '../src/runtime-profile';

function storeWithProfile(): ProfileStore {
  const store = new ProfileStore(mkdtempSync(join(tmpdir(), 'desktop-apply-')));
  store.write({ ...defaultProfile(), models_dir: 'D:/antigo' }, null); // version 2
  return store;
}

function deps(store: ProfileStore, overrides: Partial<ApplyDeps> = {}) {
  const restart = vi.fn(async (_profile: RuntimeProfile) => undefined);
  const startManaged = vi.fn(async (_profile: RuntimeProfile) => undefined);
  const scheduleReload = vi.fn();
  const base: ApplyDeps = {
    store,
    blockers: async () => [],
    restart,
    startManaged,
    scheduleReload,
    log: () => undefined,
    ...overrides,
  };
  return { deps: base, restart, startManaged, scheduleReload };
}

describe('applyProfileDraft', () => {
  it('applies: writes the new version, restarts once and reloads the page', async () => {
    const store = storeWithProfile();
    const { deps: d, restart, scheduleReload } = deps(store);
    const outcome = await applyProfileDraft({ models_dir: 'D:/novo' }, 2, { force: false }, d);

    expect(outcome).toEqual({ status: 'applied', applied_version: 3, blockers: [], error: null });
    expect(store.read().models_dir).toBe('D:/novo');
    expect(restart).toHaveBeenCalledTimes(1);
    expect(restart.mock.calls[0][0].models_dir).toBe('D:/novo');
    expect(scheduleReload).toHaveBeenCalledTimes(1);
  });

  it('defers without touching the profile or the process while work is running', async () => {
    const store = storeWithProfile();
    const before = readFileSync(store.path, 'utf8');
    const { deps: d, restart, startManaged } = deps(store, {
      blockers: async () => ['importações ativas: 2'],
    });
    const outcome = await applyProfileDraft({ models_dir: 'D:/novo' }, 2, { force: false }, d);

    expect(outcome.status).toBe('deferred');
    expect(outcome.blockers).toEqual(['importações ativas: 2']);
    expect(outcome.applied_version).toBeNull();
    expect(readFileSync(store.path, 'utf8')).toBe(before);
    expect(restart).not.toHaveBeenCalled();
    expect(startManaged).not.toHaveBeenCalled();
  });

  it('applies over blockers only when the operator forces it', async () => {
    const store = storeWithProfile();
    const { deps: d, restart } = deps(store, { blockers: async () => ['na fila: 4'] });
    const outcome = await applyProfileDraft({ models_dir: 'D:/novo' }, 2, { force: true }, d);
    expect(outcome.status).toBe('applied');
    expect(restart).toHaveBeenCalledTimes(1);
  });

  it('rolls back the file and restarts the previous profile when the new one fails', async () => {
    const store = storeWithProfile();
    const previousRaw = readFileSync(store.path, 'utf8');
    const restart = vi
      .fn<(profile: RuntimeProfile) => Promise<void>>()
      .mockRejectedValueOnce(new Error('backend não iniciou'))
      .mockResolvedValueOnce(undefined);
    const startManaged = vi.fn(async () => undefined);
    const scheduleReload = vi.fn();
    const outcome = await applyProfileDraft(
      { models_dir: 'D:/novo' },
      2,
      { force: false },
      { store, blockers: async () => [], restart, startManaged, scheduleReload, log: () => undefined },
    );

    expect(outcome.status).toBe('rolled_back');
    expect(outcome.error).toContain('backend não iniciou');
    expect(outcome.applied_version).toBeNull();
    // The bytes are the previous ones, and the previous environment was restarted.
    expect(readFileSync(store.path, 'utf8')).toBe(previousRaw);
    expect(store.read().version).toBe(2);
    expect(restart).toHaveBeenCalledTimes(2);
    expect(restart.mock.calls[1][0].models_dir).toBe('D:/antigo');
    expect(scheduleReload).toHaveBeenCalledTimes(1);
  });

  it('reports rolled_back even when the restore restart also fails', async () => {
    const store = storeWithProfile();
    const log = vi.fn();
    const restart = vi.fn(async () => {
      throw new Error('sem backend');
    });
    const outcome = await applyProfileDraft(
      { models_dir: 'D:/novo' },
      2,
      { force: true },
      { store, blockers: async () => [], restart, startManaged: async () => undefined, scheduleReload: () => undefined, log },
    );
    expect(outcome.status).toBe('rolled_back');
    expect(log).toHaveBeenCalledWith(expect.stringContaining('também falhou'));
  });

  it('rolls back when the managed Ollama cannot start', async () => {
    const store = storeWithProfile();
    const { deps: d, restart } = deps(store, {
      startManaged: async () => {
        throw new Error('ollama.exe ausente');
      },
    });
    const outcome = await applyProfileDraft({ mode: 'managed', ollama_path: 'C:/x/ollama.exe' }, 2, { force: false }, d);
    expect(outcome.status).toBe('rolled_back');
    expect(outcome.error).toContain('ollama.exe ausente');
    // Restarted once with the new profile, once with the restored one.
    expect(restart).toHaveBeenCalledTimes(2);
    expect(store.read().mode).toBe('external');
  });

  it('rejects a stale expected version before anything changes', async () => {
    const store = storeWithProfile();
    const before = readFileSync(store.path, 'utf8');
    const { deps: d, restart } = deps(store);
    await expect(applyProfileDraft({ models_dir: 'D:/novo' }, 1, { force: false }, d)).rejects.toBeInstanceOf(
      ProfileError,
    );
    expect(readFileSync(store.path, 'utf8')).toBe(before);
    expect(restart).not.toHaveBeenCalled();
  });

  it('rejects an invalid draft before asking the backend about activity', async () => {
    const store = storeWithProfile();
    const blockers = vi.fn(async () => []);
    const { deps: d } = deps(store, { blockers });
    await expect(applyProfileDraft({ mode: 'managed', ollama_path: null }, 2, { force: false }, d)).rejects.toThrow(
      /ollama\.exe/,
    );
    expect(blockers).not.toHaveBeenCalled();
  });
});

describe('runtime activity blockers', () => {
  it('turns non-zero counters into readable blockers and ignores zeros', () => {
    expect(
      summarizeActivity({ queued: 0, running: 2, waiting_children: 0, active_imports: 1, indexing: 0, total: 3 }),
    ).toEqual(['em execução: 2', 'importações ativas: 1']);
    expect(summarizeActivity({ total: 0, queued: 0 })).toEqual([]);
  });

  it('treats an unreachable or malformed activity answer as a blocker', async () => {
    const unreachable = await fetchRuntimeBlockers({
      baseUrl: 'http://127.0.0.1:1',
      headers: {},
      fetchImpl: async () => {
        throw new Error('ECONNREFUSED');
      },
    });
    expect(unreachable).toEqual(['estado de atividade indisponível']);

    const malformed = await fetchRuntimeBlockers({
      baseUrl: 'http://127.0.0.1:1',
      headers: {},
      fetchImpl: async () => new Response('null', { status: 200 }),
    });
    expect(malformed).toEqual(['estado de atividade indisponível']);
  });

  it('sends the host headers and reads the counters', async () => {
    const seen: Array<{ url: string; headers: unknown }> = [];
    const result = await fetchRuntimeBlockers({
      baseUrl: 'http://127.0.0.1:40001',
      headers: { 'x-internal-token': 't', 'x-desktop-host': 'h' },
      fetchImpl: (async (url: string, init: { headers: unknown }) => {
        seen.push({ url, headers: init.headers });
        return new Response(JSON.stringify({ queued: 1, total: 1 }), { status: 200 });
      }) as unknown as typeof fetch,
    });
    expect(seen[0].url).toBe('http://127.0.0.1:40001/internal/v1/runtime/activity');
    expect(seen[0].headers).toEqual({ 'x-internal-token': 't', 'x-desktop-host': 'h' });
    expect(result).toEqual(['na fila: 1']);
  });
});
