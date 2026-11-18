/**
 * Client-side queue of documents waiting to be uploaded.
 *
 * The queue is intentionally unbounded: the product has no per-batch file
 * limit, only the client-side transfer concurrency limit in
 * `@/services/transfer`. Metadata (never the `File` bytes) is mirrored to
 * IndexedDB so a reload keeps the list; items without bytes become
 * `needs_reselection`.
 */

import { create } from 'zustand';
import type { ConversionOptionsV2 } from '@pdf2x/contracts';
import { defaultConversionOptions } from '@pdf2x/contracts';
import {
  loadPersistedUploads,
  savePersistedUploads,
  type PersistedUploadItem,
  type PersistedUploadState,
} from '@/lib/upload-persistence';

export type UploadItemState = PersistedUploadState;

export interface UploadItem {
  clientItemId: string;
  file?: File;
  filename: string;
  size: number;
  kind: 'pdf' | 'image';
  options: ConversionOptionsV2;
  state: UploadItemState;
  uploadId?: string;
  conversionId?: string;
  uploadedBytes?: number;
  error?: string;
}

export type UploadItemPatch = Partial<Omit<UploadItem, 'clientItemId'>>;

interface UploadQueueState {
  items: UploadItem[];
  hydrated: boolean;
  enqueueFiles: (files: File[], defaults: ConversionOptionsV2) => void;
  setFile: (clientItemId: string, file: File) => void;
  removeItem: (clientItemId: string) => void;
  setItemState: (clientItemId: string, patch: UploadItemPatch) => void;
  clearFinished: () => void;
  reset: () => void;
  hydrate: () => Promise<void>;
}

let fallbackCounter = 0;

/** Stable client-side identity, generated once per file. */
function newClientItemId(): string {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID();
    }
  } catch {
    // ignore
  }
  fallbackCounter += 1;
  return `item-${Date.now().toString(36)}-${fallbackCounter.toString(36)}-${Math.random()
    .toString(36)
    .slice(2, 8)}`;
}

function kindFromName(name: string): 'pdf' | 'image' {
  return /\.pdf$/i.test(name.trim()) ? 'pdf' : 'image';
}

function cloneOptions(options: ConversionOptionsV2): ConversionOptionsV2 {
  return {
    schema_version: options.schema_version,
    target_formats: [...options.target_formats],
    ocr: { ...options.ocr, languages: [...options.ocr.languages] },
    text: { ...options.text },
    csv: { ...options.csv, table_ids: options.csv.table_ids ? [...options.csv.table_ids] : null },
  };
}

function optionsForKind(defaults: ConversionOptionsV2, kind: 'pdf' | 'image'): ConversionOptionsV2 {
  const base = cloneOptions(defaults ?? defaultConversionOptions(kind));
  // An image can only be delivered as TXT; asking for xlsx/csv is a 400.
  if (kind === 'image') return { ...base, target_formats: ['txt'] };
  return base;
}

function metadataOf(items: readonly UploadItem[]): PersistedUploadItem[] {
  return items.map((item) => ({
    clientItemId: item.clientItemId,
    filename: item.filename,
    size: item.size,
    kind: item.kind,
    options: item.options,
    state: item.state,
    uploadId: item.uploadId,
    conversionId: item.conversionId,
    uploadedBytes: item.uploadedBytes,
  }));
}

function persist(items: readonly UploadItem[]): void {
  savePersistedUploads(metadataOf(items));
}

// A reset (or a new queue) supersedes an in-flight hydration.
let hydrationGeneration = 0;

export const useUploadQueue = create<UploadQueueState>((set, get) => ({
  items: [],
  hydrated: false,

  enqueueFiles: (files, defaults) => {
    if (!files || files.length === 0) return;
    const added: UploadItem[] = files.map((file) => {
      const kind = kindFromName(file.name);
      return {
        clientItemId: newClientItemId(),
        file,
        filename: file.name,
        size: file.size,
        kind,
        options: optionsForKind(defaults, kind),
        state: 'queued',
      };
    });
    set((state) => ({ items: [...state.items, ...added] }));
    persist(get().items);
  },

  setFile: (clientItemId, file) => {
    const kind = kindFromName(file.name);
    set((state) => ({
      items: state.items.map((item) =>
        item.clientItemId === clientItemId
          ? {
              ...item,
              file,
              filename: file.name,
              size: file.size,
              kind,
              options: optionsForKind(item.options, kind),
              state: 'queued',
              uploadedBytes: undefined,
              error: undefined,
            }
          : item,
      ),
    }));
    persist(get().items);
  },

  removeItem: (clientItemId) => {
    set((state) => ({
      items: state.items.filter((item) => item.clientItemId !== clientItemId),
    }));
    persist(get().items);
  },

  setItemState: (clientItemId, patch) => {
    set((state) => ({
      items: state.items.map((item) => {
        if (item.clientItemId !== clientItemId) return item;
        const next: UploadItem = { ...item, ...patch };
        // A successful transition should not keep a stale error visible.
        if (patch.state && patch.state !== 'failed' && patch.error === undefined) {
          next.error = undefined;
        }
        return next;
      }),
    }));
    // Byte-progress updates arrive once per part; persisting each one would
    // hammer IndexedDB while a large document streams. Lifecycle transitions
    // (state, uploadId, conversionId) are still persisted immediately.
    const progressOnly = Object.keys(patch).length === 1 && 'uploadedBytes' in patch;
    if (!progressOnly) persist(get().items);
  },

  clearFinished: () => {
    set((state) => ({
      items: state.items.filter((item) => item.state !== 'uploaded' && item.state !== 'cancelled'),
    }));
    persist(get().items);
  },

  reset: () => {
    hydrationGeneration += 1;
    set({ items: [], hydrated: true });
    persist([]);
  },

  hydrate: async () => {
    const generation = hydrationGeneration;
    const persisted = await loadPersistedUploads();
    if (generation !== hydrationGeneration) return;

    set((state) => {
      const known = new Set(state.items.map((item) => item.clientItemId));
      const restored: UploadItem[] = persisted
        .filter((item) => !known.has(item.clientItemId))
        .map((item) => ({
          clientItemId: item.clientItemId,
          filename: item.filename,
          size: item.size,
          kind: item.kind,
          options: item.options,
          state:
            item.state === 'uploading' || item.state === 'queued'
              ? 'needs_reselection'
              : item.state,
          uploadId: item.uploadId,
          conversionId: item.conversionId,
          uploadedBytes: item.uploadedBytes,
        }));
      return { items: [...state.items, ...restored], hydrated: true };
    });
    persist(get().items);
  },
}));

// Hydrate once when the module loads in a browser. jsdom has no IndexedDB, so
// unit tests are unaffected.
if (typeof indexedDB !== 'undefined') {
  void useUploadQueue.getState().hydrate();
}
