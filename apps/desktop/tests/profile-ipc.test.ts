import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import {
  IpcError,
  assertSelectionShape,
  assertTrustedSender,
  validateApplyArgs,
  validateModelPickArgs,
  validateSourcePickArgs,
} from '../src/ipc-handlers';
import { isAbsolutePath } from '../src/local-grants';
import {
  ProfileError,
  ProfileStore,
  assertLoopbackEndpoint,
  defaultProfile,
  validateDraft,
} from '../src/runtime-profile';

function tempDir(): string {
  return mkdtempSync(join(tmpdir(), 'desktop-profile-'));
}

describe('ProfileStore', () => {
  it('starts from documented defaults when no file exists', () => {
    const store = new ProfileStore(tempDir());
    const profile = store.read();
    expect(profile.mode).toBe('external');
    expect(profile.ocr.model).toBe('glm-ocr:latest');
    expect(profile.assistant.model).toBe('qwen3.5:4b');
    expect(profile.preferences.close_to_tray).toBe(true);
    expect(profile.preferences.launch_at_login).toBe(false);
  });

  it('writes atomically and bumps the revision', () => {
    const dir = tempDir();
    const store = new ProfileStore(dir);
    const created = store.write(defaultProfile(), null);
    expect(created.version).toBe(2);
    const onDisk = JSON.parse(readFileSync(store.path, 'utf8')) as { version: number };
    expect(onDisk.version).toBe(2);
  });

  it('rejects a stale expected version instead of overwriting', () => {
    const store = new ProfileStore(tempDir());
    store.write(defaultProfile(), null); // version 2
    const current = store.read();
    expect(current.version).toBe(2);
    expect(() => store.write(defaultProfile(), 1)).toThrowError(ProfileError);
    // The stored profile is untouched.
    expect(store.read().version).toBe(2);
  });

  it('recovers from a corrupt profile without bricking the app', () => {
    const dir = tempDir();
    const store = new ProfileStore(dir);
    writeFileSync(store.path, '{ isto não é json');
    const profile = store.read();
    expect(profile.mode).toBe('external');
  });

  it('redacts absolute paths from the renderer view', () => {
    const store = new ProfileStore(tempDir());
    store.write({ ...defaultProfile(), models_dir: 'C:/modelos', ollama_path: 'C:/ollama.exe' }, null);
    const redacted = store.redacted() as Record<string, unknown>;
    expect(JSON.stringify(redacted)).not.toContain('C:/modelos');
    expect(redacted.models_dir_configured).toBe(true);
  });
});

describe('profile validation', () => {
  it('only accepts loopback endpoints in this version', () => {
    expect(() => assertLoopbackEndpoint('127.0.0.1')).not.toThrow();
    expect(() => assertLoopbackEndpoint('localhost')).not.toThrow();
    expect(() => assertLoopbackEndpoint('modelos.example.com')).toThrowError(ProfileError);
  });

  it('requires an executable in managed mode', () => {
    const managed = { ...defaultProfile(), mode: 'managed' as const, ollama_path: null };
    expect(() => validateDraft(managed)).toThrowError(/ollama\.exe/);
  });

  it('rejects an invalid port', () => {
    const draft = defaultProfile();
    draft.ocr.endpoint_port = -1;
    expect(() => validateDraft(draft)).toThrowError(ProfileError);
  });
});

describe('IPC validation', () => {
  const origin = 'http://127.0.0.1:41234';

  it('refuses a foreign sender frame', () => {
    expect(() => assertTrustedSender({ senderFrame: { url: 'http://evil.test/' } }, origin)).toThrowError(
      IpcError,
    );
    expect(() => assertTrustedSender({ senderFrame: null }, origin)).toThrowError(IpcError);
    expect(() => assertTrustedSender({ senderFrame: { url: `${origin}/sources` } }, origin)).not.toThrow();
  });

  it('validates pick arguments', () => {
    expect(validateSourcePickArgs('directory')).toBe('directory');
    expect(() => validateSourcePickArgs('everything')).toThrowError(IpcError);
    expect(validateModelPickArgs('gguf')).toBe('gguf');
    expect(() => validateModelPickArgs('checkpoint')).toThrowError(IpcError);
  });

  it('validates apply arguments', () => {
    expect(validateApplyArgs({ mode: 'external' }, 3).expectedVersion).toBe(3);
    expect(validateApplyArgs({}, null).expectedVersion).toBeNull();
    expect(() => validateApplyArgs([], 1)).toThrowError(IpcError);
    expect(() => validateApplyArgs({}, -2)).toThrowError(IpcError);
  });

  it('refuses a selection whose type does not match the request', () => {
    const dir = tempDir();
    const file = join(dir, 'model.gguf');
    writeFileSync(file, 'x');
    expect(assertSelectionShape(dir, 'models_directory')).toBe(dir);
    expect(() => assertSelectionShape(dir, 'gguf')).toThrowError(IpcError);
    expect(assertSelectionShape(file, 'gguf')).toBe(file);
    expect(() => assertSelectionShape(file, 'models_directory')).toThrowError(IpcError);
  });

  it('recognises absolute paths for grants', () => {
    expect(isAbsolutePath('C:\\Dados')).toBe(true);
    expect(isAbsolutePath('/home/user')).toBe(true);
    expect(isAbsolutePath('dados/pasta')).toBe(false);
  });
});
