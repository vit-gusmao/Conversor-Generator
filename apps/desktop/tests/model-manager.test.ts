import { EventEmitter } from 'node:events';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { describe, expect, it, vi } from 'vitest';

import {
  ManagedOllama,
  ModelManagerError,
  ollamaCreateArgs,
  planGgufImport,
  tagDigest,
} from '../src/model-manager';

class FakeProcess extends EventEmitter {
  killed = false;
  kill(): boolean {
    this.killed = true;
    this.emit('exit', 0);
    return true;
  }
}

function tempFile(name: string, content = 'x'): string {
  const dir = mkdtempSync(join(tmpdir(), 'ollama-'));
  const path = join(dir, name);
  writeFileSync(path, content);
  return path;
}

describe('ManagedOllama', () => {
  it('refuses a missing executable', () => {
    const manager = new ManagedOllama({ executable: join(tmpdir(), 'nao-existe.exe'), modelsDir: null });
    expect(() => manager.validateExecutable()).toThrowError(ModelManagerError);
  });

  it('starts with OLLAMA_MODELS only in the owned process', async () => {
    const executable = tempFile(process.platform === 'win32' ? 'ollama.exe' : 'ollama');
    const process_ = new FakeProcess();
    const spawnImpl = vi.fn(() => process_) as unknown as typeof import('node:child_process').spawn;
    const manager = new ManagedOllama({
      executable,
      modelsDir: mkdtempSync(join(tmpdir(), 'models-')),
      spawnImpl,
      port: 11500,
    });
    await manager.start();
    const options = (spawnImpl as unknown as { mock: { calls: unknown[][] } }).mock.calls[0][2] as {
      env: Record<string, string>;
      shell: boolean;
    };
    expect(options.shell).toBe(false);
    expect(options.env.OLLAMA_MODELS).toBeTruthy();
    expect(options.env.OLLAMA_HOST).toBe('127.0.0.1:11500');
    // Windows-wide env untouched.
    expect(process.env.OLLAMA_MODELS).toBeUndefined();
  });

  it('stop() only terminates the process it started', async () => {
    const executable = tempFile(process.platform === 'win32' ? 'ollama.exe' : 'ollama');
    const process_ = new FakeProcess();
    const manager = new ManagedOllama({
      executable,
      modelsDir: null,
      spawnImpl: vi.fn(() => process_) as unknown as typeof import('node:child_process').spawn,
    });
    await manager.start();
    await manager.stop(500);
    expect(process_.killed).toBe(true);
  });

  it('refuses a missing models directory', () => {
    const executable = tempFile(process.platform === 'win32' ? 'ollama.exe' : 'ollama');
    const manager = new ManagedOllama({ executable, modelsDir: join(tmpdir(), 'sem-pasta-xyz') });
    expect(() => manager.validateExecutable()).toThrowError(/pasta de modelos/);
  });
});

describe('GGUF import', () => {
  it('generates its own Modelfile with a fixed argument list', () => {
    const gguf = tempFile('modelo.gguf', 'weights');
    const plan = planGgufImport(gguf, 'meu-modelo:local');
    const args = ollamaCreateArgs(plan);
    expect(args).toEqual(['create', 'meu-modelo:local', '-f', plan.modelfile]);
    plan.cleanup();
  });

  it('rejects non-gguf files', () => {
    const notGguf = tempFile('modelo.bin');
    expect(() => planGgufImport(notGguf, 'tag')).toThrowError(ModelManagerError);
  });

  it('rejects unsafe tags', () => {
    const gguf = tempFile('m.gguf');
    expect(() => planGgufImport(gguf, 'tag com espaço')).toThrowError(ModelManagerError);
    expect(() => planGgufImport(gguf, '')).toThrowError(ModelManagerError);
  });
});

describe('tagDigest', () => {
  it('returns the digest of the selected tag', async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ models: [{ name: 'glm-ocr:latest', digest: 'sha256:abc' }] }),
    }) as unknown as typeof fetch;
    expect(await tagDigest('http://127.0.0.1:11434', 'glm-ocr:latest', fetchImpl)).toBe('sha256:abc');
  });

  it('returns null when the runtime is unreachable', async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new Error('offline')) as unknown as typeof fetch;
    expect(await tagDigest('http://127.0.0.1:11434', 'x', fetchImpl)).toBeNull();
  });
});
