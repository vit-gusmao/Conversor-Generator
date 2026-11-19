import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { ProfileError, ProfileStore, defaultProfile, mergeDraft } from '../src/runtime-profile';

function tempDir(): string {
  return mkdtempSync(join(tmpdir(), 'desktop-merge-'));
}

describe('mergeDraft (F5: null-safe merge)', () => {
  const saved = {
    ...defaultProfile(),
    mode: 'managed' as const,
    ollama_path: 'C:/Programas/ollama.exe',
    models_dir: 'D:/modelos',
  };

  it('keeps saved paths when the draft omits them or sends null', () => {
    const merged = mergeDraft(saved, { mode: 'managed', ollama_path: null, models_dir: undefined });
    expect(merged.ollama_path).toBe('C:/Programas/ollama.exe');
    expect(merged.models_dir).toBe('D:/modelos');
  });

  it('clears a path only with an empty string or an explicit clear flag', () => {
    expect(mergeDraft(saved, { models_dir: '' }).models_dir).toBeNull();
    expect(mergeDraft(saved, { clear_models_dir: true }).models_dir).toBeNull();
    expect(mergeDraft(saved, { clear_ollama_path: true }).ollama_path).toBeNull();
    // A flag that is not literally true does not clear anything.
    expect(mergeDraft(saved, { clear_ollama_path: 'yes' }).ollama_path).toBe('C:/Programas/ollama.exe');
  });

  it('sets a new path and merges nested sections field by field', () => {
    const merged = mergeDraft(saved, {
      ollama_path: 'E:/ollama.exe',
      ocr: { enabled: false, model: undefined, endpoint_port: 11500 },
    });
    expect(merged.ollama_path).toBe('E:/ollama.exe');
    expect(merged.ocr.enabled).toBe(false);
    expect(merged.ocr.model).toBe('glm-ocr:latest'); // undefined never overwrites
    expect(merged.ocr.endpoint_port).toBe(11500);
    expect(merged.assistant).toEqual(saved.assistant);
  });

  it('ignores host-owned and unknown keys', () => {
    const merged = mergeDraft(saved, {
      version: 99,
      data_dir: 'C:/evil',
      work_dir: 'C:/evil',
      unexpected: true,
    } as Record<string, unknown>);
    expect(merged.version).toBe(saved.version);
    expect(merged.data_dir).toBeNull();
    expect(merged.work_dir).toBeNull();
    expect((merged as unknown as Record<string, unknown>).unexpected).toBeUndefined();
  });

  it('rejects an unknown mode and non-text paths', () => {
    expect(() => mergeDraft(saved, { mode: 'remote' })).toThrowError(ProfileError);
    expect(() => mergeDraft(saved, { models_dir: 42 })).toThrowError(ProfileError);
  });

  it('does not mutate the stored profile object', () => {
    const copy = JSON.parse(JSON.stringify(saved));
    mergeDraft(saved, { ocr: { enabled: false }, models_dir: '' });
    expect(saved).toEqual(copy);
  });
});

describe('ProfileStore: redaction and rollback snapshot', () => {
  it('redacts paths into presence flags, including the Ollama executable', () => {
    const store = new ProfileStore(tempDir());
    store.write({ ...defaultProfile(), ollama_path: 'C:/ollama.exe', models_dir: null }, null);
    const redacted = store.redacted() as Record<string, unknown>;
    expect(redacted.ollama_path_configured).toBe(true);
    expect(redacted.models_dir_configured).toBe(false);
    expect(JSON.stringify(redacted)).not.toContain('C:/ollama.exe');
  });

  it('restores the exact previous bytes and version on rollback', () => {
    const store = new ProfileStore(tempDir());
    store.write({ ...defaultProfile(), models_dir: 'D:/a' }, null);
    const snapshot = store.snapshotRaw();
    expect(snapshot).not.toBeNull();
    store.write({ ...defaultProfile(), models_dir: 'D:/b' }, 2);
    expect(store.read().version).toBe(3);

    store.restoreRaw(snapshot);
    expect(store.read().version).toBe(2);
    expect(store.read().models_dir).toBe('D:/a');
    expect(readFileSync(store.path, 'utf8')).toBe(snapshot);
  });

  it('removes the profile file when there was none before the apply', () => {
    const store = new ProfileStore(tempDir());
    expect(store.snapshotRaw()).toBeNull();
    store.write(defaultProfile(), null);
    store.restoreRaw(null);
    expect(store.read().version).toBe(1); // back to defaults
  });

  it('keeps the stored file untouched when the expected version is stale', () => {
    const dir = tempDir();
    const store = new ProfileStore(dir);
    writeFileSync(store.path, JSON.stringify({ ...defaultProfile(), version: 5 }));
    const before = readFileSync(store.path, 'utf8');
    expect(() => store.write(defaultProfile(), 4)).toThrowError(ProfileError);
    expect(readFileSync(store.path, 'utf8')).toBe(before);
  });
});
