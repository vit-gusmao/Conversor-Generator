/**
 * Local root grants (T12, doc 02 §4).
 *
 * Conceding a disk root is an administrative LOCAL action. The desktop proves
 * it with the per-boot bootstrap secret the backend generated; a web page on
 * the same machine cannot obtain that secret, so it cannot grant roots.
 */

import { randomBytes } from 'node:crypto';
import { EventEmitter } from 'node:events';
import { posix, win32 } from 'node:path';

export interface GrantRequest {
  ownerId: string;
  canonicalPath: string;
  label?: string | null;
}

export interface GrantResult {
  rootId: string;
  canonicalPath: string;
}

export class GrantError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = 'GrantError';
    this.code = code;
  }
}

export interface GrantClientOptions {
  baseUrl: string;
  /** Internal token of THIS backend boot (never exposed to the renderer). */
  bootstrapSecret: string;
  /** Desktop host secret of THIS backend process (x-desktop-host). */
  hostSecret: string;
  fetchImpl?: typeof fetch;
}

export class LocalGrants extends EventEmitter {
  private readonly fetchImpl: typeof fetch;

  constructor(private readonly options: GrantClientOptions) {
    super();
    this.fetchImpl = options.fetchImpl ?? fetch;
  }

  static newSecret(): string {
    return randomBytes(32).toString('base64url');
  }

  async grant(request: GrantRequest): Promise<GrantResult> {
    if (!request.ownerId) {
      throw new GrantError('GRANT_INVALID', 'ownerId ausente.');
    }
    if (!request.canonicalPath || !isAbsolutePath(request.canonicalPath)) {
      throw new GrantError('GRANT_INVALID', 'A raiz deve ser um caminho absoluto.');
    }
    const response = await this.fetchImpl(`${this.options.baseUrl}/internal/v1/source-roots`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-internal-token': this.options.bootstrapSecret,
        'x-desktop-host': this.options.hostSecret,
      },
      body: JSON.stringify({
        owner_id: request.ownerId,
        canonical_path: request.canonicalPath,
        label: request.label ?? null,
        grant_channel: 'desktop-host',
      }),
    });
    if (!response.ok) {
      const payload = (await response.json().catch(() => ({}))) as {
        error?: { code?: string; message?: string };
      };
      throw new GrantError(
        payload.error?.code ?? 'GRANT_FAILED',
        payload.error?.message ?? 'O backend recusou a concessão da raiz.',
      );
    }
    const created = (await response.json()) as { id: string; canonical_path?: string };
    this.emit('granted', { rootId: created.id, canonicalPath: request.canonicalPath });
    return { rootId: created.id, canonicalPath: request.canonicalPath };
  }

  async revoke(ownerId: string, rootId: string): Promise<void> {
    const response = await this.fetchImpl(
      `${this.options.baseUrl}/internal/v1/source-roots/${encodeURIComponent(rootId)}`,
      {
        method: 'DELETE',
        headers: {
          'x-internal-token': this.options.bootstrapSecret,
          'x-desktop-host': this.options.hostSecret,
          'x-owner-id': ownerId,
        },
      },
    );
    if (!response.ok && response.status !== 404) {
      throw new GrantError('GRANT_REVOKE_FAILED', 'Não foi possível revogar a raiz.');
    }
  }
}

/**
 * Directory to grant for a single selected file (F16): its parent folder.
 * A file directly under a drive root keeps the root's trailing separator, so
 * the granted path is a valid absolute root ("C:\").
 */
export function parentDirectoryForGrant(
  filePath: string,
  platform: NodeJS.Platform = process.platform,
): string {
  if (platform === 'win32') {
    const parent = win32.dirname(filePath);
    return /^[a-zA-Z]:$/.test(parent) ? `${parent}\\` : parent;
  }
  return posix.dirname(filePath);
}

export function isAbsolutePath(path: string): boolean {
  if (/^[a-zA-Z]:[\\/]/.test(path)) return true; // C:\ or C:/
  if (path.startsWith('\\\\')) return true; // UNC (rejected later by the engine)
  return path.startsWith('/');
}
