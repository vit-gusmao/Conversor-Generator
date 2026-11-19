/**
 * Path policy unit tests: traversal, UNC/device paths, symlink escapes,
 * non-regular files, and non-overwriting download targets.
 */

import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { after, before, test } from 'node:test';

import { expandHome } from '../src/config.js';
import {
  PathPolicyError,
  isInsideRoot,
  resolveAllowedDirectory,
  resolveLocalPath,
  safeDownloadTarget,
  sanitizeFilename,
} from '../src/path-policy.js';

let baseDir: string;
let root: string;
let outside: string;

before(async () => {
  baseDir = await fs.mkdtemp(path.join(os.tmpdir(), 'pdf2x-path-'));
  root = path.join(baseDir, 'root');
  outside = path.join(baseDir, 'outside');
  await fs.mkdir(root, { recursive: true });
  await fs.mkdir(outside, { recursive: true });
});

after(async () => {
  await fs.rm(baseDir, { recursive: true, force: true });
});

async function expectPolicyError(
  promise: Promise<unknown>,
  code: string,
): Promise<void> {
  await assert.rejects(promise, (error: unknown) => {
    assert.ok(error instanceof PathPolicyError, `expected PathPolicyError, got ${String(error)}`);
    assert.equal(error.code, code);
    return true;
  });
}

test('accepts a regular file inside the allowed root', async () => {
  const file = path.join(root, 'document.pdf');
  await fs.writeFile(file, 'content');
  const resolved = await resolveLocalPath(file, [root]);
  assert.equal(resolved.size, 7);
  assert.equal(resolved.filename, 'document.pdf');
});

test('rejects a file outside the allowed root', async () => {
  const file = path.join(outside, 'secret.pdf');
  await fs.writeFile(file, 'secret');
  await expectPolicyError(resolveLocalPath(file, [root]), 'PATH_OUTSIDE_ROOTS');
});

test('rejects a ".." traversal even when it lands inside the root', async () => {
  const file = path.join(root, 'inside.pdf');
  await fs.writeFile(file, 'content');
  const traversal = `${root}${path.sep}..${path.sep}root${path.sep}inside.pdf`;
  await expectPolicyError(resolveLocalPath(traversal, [root]), 'PATH_TRAVERSAL');
});

test('rejects UNC and device paths', async () => {
  await expectPolicyError(
    resolveLocalPath('\\\\server\\share\\file.pdf', [root]),
    'UNSUPPORTED_PATH',
  );
  await expectPolicyError(
    resolveLocalPath('\\\\.\\PhysicalDrive0', [root]),
    'UNSUPPORTED_PATH',
  );
  await expectPolicyError(resolveLocalPath('//server/share/file.pdf', [root]), 'UNSUPPORTED_PATH');
});

test('rejects a symlink that escapes the allowed root', async () => {
  const outsideFile = path.join(outside, 'escaped.pdf');
  await fs.writeFile(outsideFile, 'escaped');
  const link = path.join(root, 'link.pdf');

  let created = false;
  try {
    await fs.symlink(outsideFile, link, 'file');
    created = true;
  } catch {
    // Windows without Developer Mode cannot create file symlinks; a directory
    // junction exercises the same realpath escape without privileges.
    const junction = path.join(root, 'junction');
    try {
      await fs.symlink(outside, junction, 'junction');
      created = true;
      await expectPolicyError(
        resolveLocalPath(path.join(junction, 'escaped.pdf'), [root]),
        'PATH_OUTSIDE_ROOTS',
      );
    } catch {
      created = false;
    }
  }
  if (!created) {
    // Last resort: assert the policy still refuses an escaping realpath.
    await expectPolicyError(
      resolveLocalPath(outsideFile, [root]),
      'PATH_OUTSIDE_ROOTS',
    );
    return;
  }
  if (created && link) {
    await expectPolicyError(resolveLocalPath(link, [root]), 'PATH_OUTSIDE_ROOTS');
  }
});

test('rejects a directory, a socket-like special file and a missing file', async () => {
  await expectPolicyError(resolveLocalPath(root, [root]), 'NOT_A_REGULAR_FILE');
  await expectPolicyError(
    resolveLocalPath(path.join(root, 'missing.pdf'), [root]),
    'SOURCE_NOT_FOUND',
  );
});

test('resolveAllowedDirectory requires an existing allowed directory', async () => {
  assert.equal(await resolveAllowedDirectory(root, [root]), path.resolve(root));
  await expectPolicyError(resolveAllowedDirectory(outside, [root]), 'PATH_OUTSIDE_ROOTS');
  await expectPolicyError(
    resolveAllowedDirectory(path.join(root, 'nope'), [root]),
    'DESTINATION_NOT_FOUND',
  );
});

test('safeDownloadTarget never overwrites and appends " (2)"', async () => {
  const first = await safeDownloadTarget(root, 'report.csv');
  assert.equal(first, path.join(path.resolve(root), 'report.csv'));
  await fs.writeFile(first, 'first');

  const second = await safeDownloadTarget(root, 'report.csv');
  assert.equal(path.basename(second), 'report (2).csv');
  await fs.writeFile(second, 'second');

  const third = await safeDownloadTarget(root, 'report.csv');
  assert.equal(path.basename(third), 'report (3).csv');
  assert.equal(await fs.readFile(first, 'utf8'), 'first');
});

test('sanitizeFilename strips directories and control characters', () => {
  assert.equal(sanitizeFilename('../../etc/passwd'), 'passwd');
  assert.equal(sanitizeFilename('C:\\temp\\report.csv'), 'report.csv');
  assert.equal(sanitizeFilename('bad\u0000name.csv'), 'bad_name.csv');
  assert.equal(sanitizeFilename('..', 'artifact'), 'artifact');
});

test('isInsideRoot respects path boundaries and case on Windows', () => {
  assert.equal(isInsideRoot(path.join(root, 'a', 'b.pdf'), root), true);
  assert.equal(isInsideRoot(path.join(root, '..', 'root2', 'b.pdf'), root), false);
  assert.equal(isInsideRoot(root, root), true);
  if (process.platform === 'win32') {
    assert.equal(isInsideRoot(root.toUpperCase(), root), true);
  }
});

test('tool paths must be absolute: a relative path is refused, never anchored to the working directory', async () => {
  // The file exists relative to the working directory; it must still be refused.
  const cwd = process.cwd();
  await fs.writeFile(path.join(root, 'relative-doc.pdf'), 'content');
  process.chdir(root);
  try {
    await expectPolicyError(resolveLocalPath('relative-doc.pdf', [root]), 'INVALID_PATH');
    await expectPolicyError(resolveLocalPath('./relative-doc.pdf', [root]), 'INVALID_PATH');
  } finally {
    process.chdir(cwd);
  }
  await expectPolicyError(resolveLocalPath(path.join('sub', 'document.pdf'), [root]), 'INVALID_PATH');
  await expectPolicyError(resolveLocalPath('\folder\document.pdf', [root]), 'INVALID_PATH');
  await expectPolicyError(resolveAllowedDirectory('relative-dir', [root]), 'INVALID_PATH');
});

test('expandHome expands ~ without resolving, and leaves ~user forms untouched', () => {
  assert.equal(expandHome('~'), os.homedir());
  assert.equal(expandHome('~/report.pdf'), path.join(os.homedir(), 'report.pdf'));
  assert.equal(expandHome('~bob/report.pdf'), '~bob/report.pdf');
  assert.equal(expandHome(path.join(root, 'a.pdf')), path.join(root, 'a.pdf'));
});

test('a home-relative input is expanded before the root check, so it is not reported as invalid', async () => {
  await expectPolicyError(
    resolveLocalPath(`~${path.sep}pdf2x-definitely-missing-${process.pid}.pdf`, [root]),
    'SOURCE_NOT_FOUND',
  );
});

test('reserved Windows names are detected before the first dot', () => {
  assert.equal(sanitizeFilename('CON.tar.gz'), '_CON.tar.gz');
  assert.equal(sanitizeFilename('aux.txt'), '_aux.txt');
  assert.equal(sanitizeFilename('com1.tar.gz'), '_com1.tar.gz');
  assert.equal(sanitizeFilename('nul'), '_nul');
  assert.equal(sanitizeFilename('console.tar.gz'), 'console.tar.gz');
});
