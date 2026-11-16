export interface StoredObject {
  key: string;
  size: number;
}

/**
 * Storage port. Implementations: local filesystem (default) and S3/MinIO.
 * Keys are content addressed (`ab/abcdef...pdf`) so writes are idempotent.
 */
export interface StorageService {
  readonly backend: 'local' | 's3';
  put(key: string, data: Buffer, contentType: string): Promise<StoredObject>;
  get(key: string): Promise<Buffer>;
  delete(key: string): Promise<void>;
  exists(key: string): Promise<boolean>;
}

export const STORAGE_SERVICE = 'STORAGE_SERVICE';

/**
 * Owner-scoped, content-addressed key.
 *
 * The owner prefix is part of the address: two users uploading the same bytes
 * get independent objects, so deleting one document can never break the other
 * and a hash is never usable as a global public identifier.
 */
export function storageKeyForSha256(
  sha256: string,
  extension = 'pdf',
  ownerId?: string,
): string {
  const safe = sha256.toLowerCase().replace(/[^a-f0-9]/g, '');
  if (safe.length < 4) throw new Error('invalid sha256');
  const scope = ownerId ? `documents/${ownerId.replace(/[^A-Za-z0-9_-]/g, '_')}/` : '';
  return `${scope}${safe.slice(0, 2)}/${safe}.${extension}`;
}
