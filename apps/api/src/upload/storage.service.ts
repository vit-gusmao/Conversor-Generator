import { Readable } from 'node:stream';

export interface StoredObject {
  key: string;
  size: number;
}

/**
 * Storage port. Implementations: local filesystem (default) and S3/MinIO.
 * Keys are content addressed (`ab/abcdef...pdf`) so writes are idempotent.
 *
 * The streaming methods exist so a document never has to fit in memory: an
 * upload is written part by part and assembled on disk/object storage.
 */
export interface StorageService {
  readonly backend: 'local' | 's3';
  put(key: string, data: Buffer, contentType: string): Promise<StoredObject>;
  get(key: string): Promise<Buffer>;
  delete(key: string): Promise<void>;
  exists(key: string): Promise<boolean>;
  /** Writes a stream under `key`, hashing while it copies. */
  putStream(key: string, stream: Readable, contentType: string): Promise<StoredObject>;
  /** Readable stream for a stored object, without buffering it. */
  openRead(key: string): Promise<Readable>;
  /** Size in bytes; `-1` when unknown. */
  size(key: string): Promise<number>;
  /** Moves an object; the source may be removed afterwards. */
  move(sourceKey: string, targetKey: string): Promise<void>;
  /** Concatenates confirmed parts into one immutable object. */
  compose(partKeys: string[], targetKey: string, contentType: string): Promise<StoredObject>;
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

/** Key layout for resumable upload parts. Server-generated, never client input. */
export function uploadPartKey(uploadId: string, partNumber: number): string {
  return `uploads/${uploadId}/${String(partNumber).padStart(5, '0')}.part`;
}

export function uploadIncomingKey(uploadId: string, partNumber: number, nonce: string): string {
  return `uploads/${uploadId}/incoming/${String(partNumber).padStart(5, '0')}-${nonce}.part`;
}

export function uploadStagingKey(uploadId: string): string {
  return `uploads/${uploadId}/assembled.bin`;
}
