/**
 * Filesystem policy for every path that reaches the OS.
 *
 * A tool argument is untrusted input even when the model looks trustworthy:
 * extracted document content is data and must never steer a path or a command.
 * This module is the single gate for reading source documents and writing
 * artifacts. It never shells out and never follows a path outside the
 * configured roots.
 */

import { promises as fs } from 'node:fs';
import path from 'node:path';

import { expandHome, expandPath } from './config.js';

export class PathPolicyError extends Error {
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(code: string, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.name = 'PathPolicyError';
    this.code = code;
    this.details = details;
  }
}

export interface ResolvedLocalFile {
  /** Canonical absolute path (symlinks resolved). */
  path: string;
  size: number;
  filename: string;
}

const WINDOWS_RESERVED = new Set([
  'con',
  'prn',
  'aux',
  'nul',
  'com1',
  'com2',
  'com3',
  'com4',
  'com5',
  'com6',
  'com7',
  'com8',
  'com9',
  'lpt1',
  'lpt2',
  'lpt3',
  'lpt4',
  'lpt5',
  'lpt6',
  'lpt7',
  'lpt8',
  'lpt9',
]);

function isWindows(): boolean {
  return process.platform === 'win32';
}

/** libuv may hand back an extended-length prefix; compare canonical paths. */
function stripExtendedPrefix(value: string): string {
  if (value.startsWith('\\\\?\\')) {
    const rest = value.slice(4);
    return /^UNC\\/i.test(rest) ? `\\\\${rest.slice(4)}` : rest;
  }
  return value;
}

/** True when `candidate` is `root` itself or lives underneath it. */
export function isInsideRoot(candidate: string, root: string): boolean {
  const normalize = (value: string): string => {
    const trimmed = value.replace(/[/\\]+$/, '');
    return isWindows() ? trimmed.toLowerCase() : trimmed;
  };
  const target = normalize(path.resolve(candidate));
  const base = normalize(path.resolve(root));
  if (target === base) return true;
  const withSeparator = base.endsWith(path.sep) ? base : `${base}${path.sep}`;
  return target.startsWith(withSeparator);
}

function assertNoTraversal(requested: string): void {
  if (requested.includes('\0')) {
    throw new PathPolicyError('INVALID_PATH', 'The path contains a NUL byte.');
  }
  const segments = requested.split(/[/\\]+/);
  if (segments.includes('..')) {
    throw new PathPolicyError('PATH_TRAVERSAL', 'The path contains a ".." segment.', {
      path: requested,
    });
  }
}

/**
 * Tool inputs must be absolute (after `~` expansion). Checked on the raw value
 * before any resolution: `path.resolve` would otherwise anchor a relative path
 * to the server's working directory without telling the caller. On Windows a
 * rooted path without a drive (`\folder\file.pdf`) is also refused, because it
 * resolves against the current drive.
 */
function assertAbsoluteInput(expanded: string, requested: string): void {
  const absolute =
    process.platform === 'win32'
      ? /^[A-Za-z]:[\\/]/.test(expanded)
      : path.isAbsolute(expanded);
  if (!absolute) {
    throw new PathPolicyError('INVALID_PATH', 'The path must be absolute (or start with ~).', {
      path: requested,
    });
  }
}

function assertNotRemotePath(requested: string): void {
  // `\\server\share` (UNC) and `\\.\` / `\\?\` (device namespace) are both
  // rejected: they reach another machine or a device, never a local document.
  if (/^\\\\/.test(requested) || /^\/\//.test(requested)) {
    throw new PathPolicyError(
      'UNSUPPORTED_PATH',
      'UNC and device paths are not allowed; use a local absolute path.',
      { path: requested },
    );
  }
  if (/^[A-Za-z]:[^\\/]/.test(requested)) {
    // Drive-relative ("C:file.txt") silently resolves against a per-drive
    // working directory; demand an absolute path instead.
    throw new PathPolicyError('UNSUPPORTED_PATH', 'Use an absolute path such as C:\\folder\\file.pdf.', {
      path: requested,
    });
  }
}

async function canonicalRoots(roots: readonly string[]): Promise<string[]> {
  const resolved: string[] = [];
  for (const root of roots) {
    let real: string;
    try {
      real = stripExtendedPrefix(await fs.realpath(expandPath(root)));
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code === 'ENOENT') {
        throw new PathPolicyError(
          'ROOT_UNAVAILABLE',
          `Allowed root does not exist: ${root}`,
          { root },
        );
      }
      throw new PathPolicyError('ROOT_UNAVAILABLE', `Allowed root is not accessible: ${root}`, {
        root,
      });
    }
    const stat = await fs.stat(real);
    if (!stat.isDirectory()) {
      throw new PathPolicyError('ROOT_UNAVAILABLE', `Allowed root is not a directory: ${root}`, {
        root,
      });
    }
    resolved.push(real);
  }
  return resolved;
}

function assertInside(realPath: string, roots: readonly string[], requested: string): void {
  if (!roots.some((root) => isInsideRoot(realPath, root))) {
    throw new PathPolicyError(
      'PATH_OUTSIDE_ROOTS',
      'The path is outside the configured allowed roots.',
      { path: requested, allowed_roots: [...roots] },
    );
  }
}

/**
 * Resolves a requested local file path to a canonical, allowed, regular file.
 *
 * Rejects: traversal, UNC/device paths, non-regular files (directories,
 * sockets, FIFOs, block/char devices), missing files and anything whose real
 * path (after symlink resolution) escapes the configured roots.
 */
export async function resolveLocalPath(
  requested: string,
  allowedRoots: readonly string[],
): Promise<ResolvedLocalFile> {
  if (typeof requested !== 'string' || requested.trim().length === 0) {
    throw new PathPolicyError('INVALID_PATH', 'A file path is required.');
  }
  const trimmed = requested.trim();
  assertNoTraversal(trimmed);
  assertNotRemotePath(trimmed);

  const expanded = expandHome(trimmed);
  assertAbsoluteInput(expanded, requested);

  const roots = await canonicalRoots(allowedRoots);

  let realPath: string;
  try {
    realPath = stripExtendedPrefix(await fs.realpath(path.resolve(expanded)));
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    if (code === 'ENOENT') {
      throw new PathPolicyError('SOURCE_NOT_FOUND', 'The file does not exist.', {
        path: requested,
      });
    }
    throw new PathPolicyError('SOURCE_UNAVAILABLE', 'The file cannot be accessed.', {
      path: requested,
    });
  }

  assertInside(realPath, roots, requested);

  const stat = await fs.stat(realPath);
  if (!stat.isFile()) {
    throw new PathPolicyError(
      'NOT_A_REGULAR_FILE',
      'Only regular files can be submitted (directories, sockets and devices are rejected).',
      { path: requested },
    );
  }

  return {
    path: realPath,
    size: stat.size,
    filename: path.basename(realPath),
  };
}

/** Resolves a destination directory and proves it is inside the allowed roots. */
export async function resolveAllowedDirectory(
  directory: string,
  allowedRoots: readonly string[],
): Promise<string> {
  if (typeof directory !== 'string' || directory.trim().length === 0) {
    throw new PathPolicyError('INVALID_PATH', 'A destination directory is required.');
  }
  const trimmed = directory.trim();
  assertNoTraversal(trimmed);
  assertNotRemotePath(trimmed);

  const expanded = expandHome(trimmed);
  assertAbsoluteInput(expanded, directory);
  const roots = await canonicalRoots(allowedRoots);

  let realDir: string;
  try {
    realDir = stripExtendedPrefix(await fs.realpath(path.resolve(expanded)));
  } catch {
    throw new PathPolicyError('DESTINATION_NOT_FOUND', 'The destination directory does not exist.', {
      path: directory,
    });
  }
  assertInside(realDir, roots, directory);

  const stat = await fs.stat(realDir);
  if (!stat.isDirectory()) {
    throw new PathPolicyError('NOT_A_DIRECTORY', 'The destination must be a directory.', {
      path: directory,
    });
  }
  return realDir;
}

/** Removes any path component from a server-provided filename. */
export function sanitizeFilename(filename: string, fallback = 'artifact'): string {
  let name = path.basename(filename.replace(/\\/g, '/'));
  name = name.replace(/[\u0000-\u001f<>:"/\\|?*]/g, '_').trim();
  name = name.replace(/^[.\s]+/, '');
  if (name.length === 0) name = fallback;
  // Windows reserves the name before the FIRST dot: `CON.tar.gz` is as
  // unusable as `CON.txt`, so the stem is not the name without its last extension.
  const stem = (name.split('.', 1)[0] ?? '').toLowerCase();
  if (WINDOWS_RESERVED.has(stem)) name = `_${name}`;
  if (name.length > 180) {
    const extension = path.extname(name).slice(0, 20);
    name = `${name.slice(0, 180 - extension.length)}${extension}`;
  }
  return name;
}

/** Inserts ` (n)` before the extension: `report.csv` -> `report (2).csv`. */
export function withSuffix(filename: string, index: number): string {
  if (index <= 1) return filename;
  const extension = path.extname(filename);
  const stem = extension ? filename.slice(0, -extension.length) : filename;
  return `${stem} (${index})${extension}`;
}

/**
 * Chooses a non-existing path for a download. Never overwrites: the second
 * artifact with the same name becomes `name (2).ext`, then `(3)`, …
 */
export async function safeDownloadTarget(
  directory: string,
  filename: string,
): Promise<string> {
  const safeName = sanitizeFilename(filename);
  for (let index = 1; index <= 999; index += 1) {
    const candidate = path.join(directory, withSuffix(safeName, index));
    if (!isInsideRoot(candidate, directory)) {
      throw new PathPolicyError('PATH_OUTSIDE_ROOTS', 'The target escapes the destination directory.');
    }
    try {
      // `lstat` also refuses to replace a dangling symlink.
      await fs.lstat(candidate);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return candidate;
      throw new PathPolicyError('DESTINATION_UNAVAILABLE', 'The destination cannot be inspected.', {
        path: candidate,
      });
    }
  }
  throw new PathPolicyError(
    'DESTINATION_CONFLICT',
    'Too many files with the same name exist in the destination directory.',
    { directory, filename: safeName },
  );
}
