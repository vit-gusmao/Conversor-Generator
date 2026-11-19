import { EventEmitter } from 'node:events';
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { describe, expect, it, vi } from 'vitest';

import { IpcError, validateApplyOptions, validateGgufTagArgs } from '../src/ipc-handlers';
import { parentDirectoryForGrant } from '../src/local-grants';
import {
  ManagedOllama,
  ModelManagerError,
  ollamaCreateArgs,
  planGgufImport,
  runGgufImport,
} from '../src/model-manager';

class FakeProcess extends EventEmitter {
  stdout = new EventEmitter();
  stderr = new EventEmitter();
  kill = vi.fn(() => true);
}

function ggufFile(): string {
  const dir = mkdtempSync(join(tmpdir(), 'gguf-src-'));
  const path = join(dir, 'modelo local.gguf');
  writeFileSync(path, 'GGUF');
  return path;
}

describe('planGgufImport: generated Modelfile only', () => {
  it('writes exactly one FROM line for the selected file and cleans up', () => {
    const source = ggufFile();
    const plan = planGgufImport(source, 'converter/ocr:latest');
    expect(readFileSync(plan.modelfile, 'utf8')).toBe(`FROM "${source}"\n`);
    plan.cleanup();
    expect(existsSync(plan.modelfile)).toBe(false);
  });

  it('refuses a missing file, a non-gguf file and an invalid tag', () => {
    expect(() => planGgufImport(join(tmpdir(), 'nao-existe.gguf'), 'x')).toThrowError(ModelManagerError);
    const txt = join(mkdtempSync(join(tmpdir(), 'gguf-txt-')), 'a.txt');
    writeFileSync(txt, 'x');
    expect(() => planGgufImport(txt, 'x')).toThrowError(/gguf/);
    let error: unknown = null;
    try {
      planGgufImport(ggufFile(), 'tag com espaço!');
    } catch (thrown) {
      error = thrown;
    }
    expect(error).toMatchObject({ code: 'TAG_INVALID' });
  });

  it('uses the fixed create arguments: create <tag> -f <Modelfile>', () => {
    const plan = planGgufImport(ggufFile(), 'converter/x:1');
    expect(ollamaCreateArgs(plan)).toEqual(['create', 'converter/x:1', '-f', plan.modelfile]);
    plan.cleanup();
  });
});

describe('runGgufImport: no shell, child-only environment, bounded progress', () => {
  it('runs ollama create with fixed args, OLLAMA_HOST for the child only, and forwards clean progress', async () => {
    const source = ggufFile();
    const plan = planGgufImport(source, 'converter/ocr:latest');
    const child = new FakeProcess();
    const spawnImpl = vi.fn(() => {
      setImmediate(() => {
        child.stdout.emit('data', Buffer.from('\x1b[2K\rpulling 10%\npulling 50%\r\n'));
        child.stderr.emit('data', Buffer.from('success\n'));
        child.emit('exit', 0);
      });
      return child;
    }) as unknown as typeof import('node:child_process').spawn;
    const progress: string[] = [];

    const result = await runGgufImport({
      executable: 'C:/ollama/ollama.exe',
      host: '127.0.0.1',
      port: 11434,
      plan,
      spawnImpl,
      onProgress: (line) => progress.push(line),
    });

    expect(result).toEqual({ tag: 'converter/ocr:latest', exitCode: 0 });
    const [executable, args, options] = (spawnImpl as unknown as { mock: { calls: unknown[][] } }).mock.calls[0] as [
      string,
      string[],
      { shell: boolean; env: Record<string, string>; stdio: unknown },
    ];
    expect(executable).toBe('C:/ollama/ollama.exe');
    expect(args).toEqual(['create', 'converter/ocr:latest', '-f', plan.modelfile]);
    expect(options.shell).toBe(false);
    expect(options.env.OLLAMA_HOST).toBe('127.0.0.1:11434');
    expect(progress).toEqual(['pulling 10%', 'pulling 50%', 'success']);
    // The parent process environment is never changed.
    expect(process.env.OLLAMA_HOST).toBeUndefined();
  });

  it('turns a non-zero exit into GGUF_IMPORT_FAILED with the last output line', async () => {
    const plan = planGgufImport(ggufFile(), 'converter/bad:1');
    const child = new FakeProcess();
    const spawnImpl = vi.fn(() => {
      setImmediate(() => {
        child.stderr.emit('data', Buffer.from('Error: invalid model file\n'));
        child.emit('exit', 1);
      });
      return child;
    }) as unknown as typeof import('node:child_process').spawn;

    await expect(
      runGgufImport({ executable: 'ollama.exe', host: '127.0.0.1', port: 11434, plan, spawnImpl }),
    ).rejects.toMatchObject({ code: 'GGUF_IMPORT_FAILED' });
  });

  it('reports a spawn failure as a typed error instead of crashing', async () => {
    const plan = planGgufImport(ggufFile(), 'converter/bad:2');
    const child = new FakeProcess();
    const spawnImpl = vi.fn(() => {
      setImmediate(() => child.emit('error', new Error('ENOENT')));
      return child;
    }) as unknown as typeof import('node:child_process').spawn;
    await expect(
      runGgufImport({ executable: 'missing.exe', host: '127.0.0.1', port: 11434, plan, spawnImpl }),
    ).rejects.toMatchObject({ code: 'GGUF_IMPORT_SPAWN_FAILED' });
  });
});

describe('ManagedOllama: owned process only', () => {
  it('starts serve without a shell, with output detached and OLLAMA_MODELS for that child only', async () => {
    const source = join(mkdtempSync(join(tmpdir(), 'ollama-bin-')), 'ollama.exe');
    writeFileSync(source, 'exe');
    const models = mkdtempSync(join(tmpdir(), 'ollama-models-'));
    const child = new FakeProcess();
    const spawnImpl = vi.fn(() => child) as unknown as typeof import('node:child_process').spawn;
    const managed = new ManagedOllama({ executable: source, modelsDir: models, host: '127.0.0.1', port: 11500, spawnImpl });
    await managed.start();

    const [, args, options] = (spawnImpl as unknown as { mock: { calls: unknown[][] } }).mock.calls[0] as [
      string,
      string[],
      { shell: boolean; stdio: unknown; env: Record<string, string> },
    ];
    expect(args).toEqual(['serve']);
    expect(options.shell).toBe(false);
    expect(options.stdio).toBe('ignore');
    expect(options.env.OLLAMA_MODELS).toBe(models);
    expect(options.env.OLLAMA_HOST).toBe('127.0.0.1:11500');
    expect(process.env.OLLAMA_MODELS).not.toBe(models);
    expect(managed.running).toBe(true);
  });

  it('survives a spawn error without an unhandled event and forgets the dead child', async () => {
    const source = join(mkdtempSync(join(tmpdir(), 'ollama-bin-')), 'ollama.exe');
    writeFileSync(source, 'exe');
    const child = new FakeProcess();
    const managed = new ManagedOllama({
      executable: source,
      modelsDir: null,
      spawnImpl: (() => child) as unknown as typeof import('node:child_process').spawn,
    });
    await managed.start();
    expect(() => child.emit('error', new Error('EACCES'))).not.toThrow();
    expect(managed.running).toBe(false);
  });

  it('stop() kills only the child it owns', async () => {
    const source = join(mkdtempSync(join(tmpdir(), 'ollama-bin-')), 'ollama.exe');
    writeFileSync(source, 'exe');
    const child = new FakeProcess();
    child.kill = vi.fn(() => {
      setImmediate(() => child.emit('exit', 0));
      return true;
    });
    const managed = new ManagedOllama({
      executable: source,
      modelsDir: null,
      spawnImpl: (() => child) as unknown as typeof import('node:child_process').spawn,
    });
    await managed.start();
    await managed.stop(1000);
    expect(child.kill).toHaveBeenCalledWith('SIGTERM');
    expect(managed.running).toBe(false);
  });
});

describe('path and argument validation for the new channels', () => {
  it('grants the parent folder, keeping drive roots absolute (F16)', () => {
    expect(parentDirectoryForGrant('C:\\dados\\modelo.xlsx', 'win32')).toBe('C:\\dados');
    expect(parentDirectoryForGrant('C:\\modelo.xlsx', 'win32')).toBe('C:\\');
    expect(parentDirectoryForGrant('D:\\a\\b\\c.txt', 'win32')).toBe('D:\\a\\b');
    expect(parentDirectoryForGrant('/home/u/x.pdf', 'linux')).toBe('/home/u');
    expect(parentDirectoryForGrant('/x.pdf', 'linux')).toBe('/');
  });

  it('accepts only the documented tag shape', () => {
    expect(validateGgufTagArgs('converter/ocr:latest')).toBe('converter/ocr:latest');
    expect(() => validateGgufTagArgs('')).toThrowError(IpcError);
    expect(() => validateGgufTagArgs('x'.repeat(81))).toThrowError(IpcError);
    expect(() => validateGgufTagArgs('a;rm -rf')).toThrowError(IpcError);
    expect(() => validateGgufTagArgs(42)).toThrowError(IpcError);
  });

  it('accepts apply options only as an object with a boolean force', () => {
    expect(validateApplyOptions(undefined)).toEqual({ force: false });
    expect(validateApplyOptions({})).toEqual({ force: false });
    expect(validateApplyOptions({ force: true })).toEqual({ force: true });
    expect(() => validateApplyOptions({ force: 'sim' })).toThrowError(IpcError);
    expect(() => validateApplyOptions(['force'])).toThrowError(IpcError);
  });
});
