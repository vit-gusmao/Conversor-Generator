/**
 * Minimal IndexedDB persistence for the upload queue metadata.
 *
 * `File`/`Blob` objects are never stored: a browser reload hands us a queue
 * without bytes, and the user must re-select those documents. Only enough
 * metadata to survive a reload and to resume an existing upload is kept.
 */

import type { ConversionOptionsV2 } from '@pdf2x/contracts';

export type PersistedUploadState =
  | 'queued'
  | 'uploading'
  | 'uploaded'
  | 'failed'
  | 'cancelled'
  | 'needs_reselection';

export interface PersistedUploadItem {
  clientItemId: string;
  filename: string;
  size: number;
  kind: 'pdf' | 'image';
  options: ConversionOptionsV2;
  state: PersistedUploadState;
  uploadId?: string;
  conversionId?: string;
  uploadedBytes?: number;
}

const DB_NAME = 'pdf2x-uploads';
const DB_VERSION = 1;
const STORE_NAME = 'items';

function hasIndexedDb(): boolean {
  try {
    return typeof indexedDB !== 'undefined' && indexedDB !== null;
  } catch {
    return false;
  }
}

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(STORE_NAME)) {
        database.createObjectStore(STORE_NAME, { keyPath: 'clientItemId' });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error('Falha ao abrir o armazenamento local.'));
  });
}

/** Reads the persisted metadata. Returns an empty list when unavailable. */
export async function loadPersistedUploads(): Promise<PersistedUploadItem[]> {
  if (!hasIndexedDb()) return [];
  try {
    const database = await openDatabase();
    return await new Promise<PersistedUploadItem[]>((resolve) => {
      const transaction = database.transaction(STORE_NAME, 'readonly');
      const request = transaction.objectStore(STORE_NAME).getAll();
      request.onsuccess = () => {
        const result = request.result as PersistedUploadItem[] | undefined;
        resolve(Array.isArray(result) ? result : []);
      };
      request.onerror = () => resolve([]);
      transaction.oncomplete = () => database.close();
      transaction.onabort = () => database.close();
    });
  } catch {
    return [];
  }
}

async function writeAll(items: PersistedUploadItem[]): Promise<void> {
  if (!hasIndexedDb()) return;
  const database = await openDatabase();
  try {
    await new Promise<void>((resolve) => {
      const transaction = database.transaction(STORE_NAME, 'readwrite');
      const store = transaction.objectStore(STORE_NAME);
      store.clear();
      for (const item of items) store.put(item);
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => resolve();
      transaction.onabort = () => resolve();
    });
  } finally {
    database.close();
  }
}

// Writes are serialized so a slow earlier transaction cannot overwrite a newer
// snapshot of the queue.
let writeChain: Promise<void> = Promise.resolve();

/** Replaces the persisted snapshot with `items` (metadata only). */
export function savePersistedUploads(items: readonly PersistedUploadItem[]): void {
  const snapshot = items.map((item) => ({ ...item }));
  writeChain = writeChain.then(() => writeAll(snapshot)).catch(() => undefined);
}

/** Waits for pending writes to settle. Mostly useful in tests. */
export function flushPersistedUploads(): Promise<void> {
  return writeChain;
}
