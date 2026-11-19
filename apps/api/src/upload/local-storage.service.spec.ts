import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { PassThrough } from 'node:stream';

import { LocalStorageService } from './local-storage.service';

describe('LocalStorageService.putStream', () => {
  let root: string;
  let storage: LocalStorageService;

  beforeEach(() => {
    root = fs.mkdtempSync(path.join(os.tmpdir(), 'pdf2x-local-'));
    storage = new LocalStorageService({ storageLocalRoot: root } as never);
  });

  afterEach(() => {
    fs.rmSync(root, { recursive: true, force: true });
  });

  function filesUnder(dir: string): string[] {
    const out: string[] = [];
    const walk = (current: string): void => {
      for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
        const full = path.join(current, entry.name);
        if (entry.isDirectory()) walk(full);
        else out.push(full);
      }
    };
    if (fs.existsSync(dir)) walk(dir);
    return out;
  }

  it('leaves no temporary or final file when the source aborts mid-write (A-14)', async () => {
    const source = new PassThrough();
    const pending = storage.putStream('uploads/u1/incoming/00000-x.part', source, 'application/octet-stream');
    source.write(Buffer.alloc(100, 0x41));
    // Abort only once putStream has attached its pipeline to the source. A fixed
    // timer raced the awaited mkdir before the pipeline existed, and the destroy
    // error then reached the source with no listener (unhandled 'error').
    const armAbort = (): void => {
      if (source.listenerCount('error') > 0) source.destroy(new Error('client gone'));
      else setImmediate(armAbort);
    };
    armAbort();

    await expect(pending).rejects.toThrow();
    expect(filesUnder(root)).toEqual([]);
  });

  it('rejects without an unhandled error when the source aborts before mkdir resolves (FX-7)', async () => {
    const source = new PassThrough();
    const pending = storage.putStream('uploads/u2/incoming/00001-y.part', source, 'application/octet-stream');
    // Destroy synchronously, with no listener wait: the abort lands before any
    // await in putStream. An unhandled 'error' here would crash the process.
    source.destroy(new Error('client gone'));

    await expect(pending).rejects.toThrow();
    expect(filesUnder(root)).toEqual([]);
  });

  it('publishes a fully written object under its final key', async () => {
    const source = new PassThrough();
    const pending = storage.putStream('uploads/u1/00000.part', source, 'application/octet-stream');
    source.end(Buffer.from('payload'));

    await expect(pending).resolves.toMatchObject({ size: 7 });
    expect((await storage.get('uploads/u1/00000.part')).toString()).toBe('payload');
  });
});
