/**
 * Client-side batch upload orchestration.
 *
 * The server owns the truth: `part_size` and `next_part` always come from the
 * registration response, so a resumed session never assumes 8 MiB. At most
 * `MAX_PARALLEL_TRANSFERS` documents transfer at once (client-side politeness);
 * the queue itself may hold any number of files.
 */

import type { BatchSummary, ConversionOptionsV2 } from '@pdf2x/contracts';
import { cancelItem, createBatch, registerItem } from '@/services/batches';
import { completeUpload, putPart } from '@/services/uploads';
import { sha256Hex } from '@/lib/sha256';
import { explainError } from '@/lib/errors';

/** Client-side politeness limit; the backend has no such limit. */
export const MAX_PARALLEL_TRANSFERS = 3;

/** Above this size the whole-file hash needs a single large allocation. */
export const LARGE_FILE_HASH_WARNING_BYTES = 64 * 1024 * 1024;

export interface TransferItem {
  clientItemId: string;
  file?: File;
  filename: string;
  size: number;
  options: ConversionOptionsV2;
}

export interface TransferItemPatch {
  state?: 'queued' | 'uploading' | 'uploaded' | 'failed' | 'cancelled' | 'needs_reselection';
  uploadId?: string;
  conversionId?: string;
  uploadedBytes?: number;
  error?: string;
}

export interface StartBatchFailure {
  clientItemId: string;
  message: string;
}

export interface StartBatchInput {
  items: readonly TransferItem[];
  /** Generated once per logical attempt; reuse it when retrying that attempt. */
  clientBatchKey?: string;
  onItemPatch?: (clientItemId: string, patch: TransferItemPatch) => void;
}

export interface StartBatchResult {
  batch: BatchSummary;
  batchId: string;
  failures: StartBatchFailure[];
}

/**
 * Abort controllers of the transfers that are queued or running, keyed by
 * `clientItemId`. Cancelling aborts the controller; the worker then stops and
 * reports nothing further, so a `cancelled` state is never overwritten by a
 * late progress or `uploaded` patch.
 */
const activeTransfers = new Map<string, AbortController>();

/**
 * Server identity of items whose registration already succeeded, keyed by
 * `clientItemId`. Cancelling such an item must also cancel it on the server,
 * otherwise it stays `pending_upload` (or its conversion runs on).
 */
const registeredItems = new Map<string, { batchId: string; itemId: string }>();

/** Best effort: the local cancel already happened, so a failure here is ignored. */
function cancelRegisteredItem(batchId: string, itemId: string): void {
  cancelItem(batchId, itemId).catch(() => undefined);
}

/**
 * Cancels a queued or running transfer. Returns `false` when the item has no
 * active transfer (never started, already finished, or already cancelled).
 * An item already registered on the server is also cancelled there (best effort).
 */
export function cancelTransfer(clientItemId: string): boolean {
  const controller = activeTransfers.get(clientItemId);
  if (!controller) return false;
  activeTransfers.delete(clientItemId);
  controller.abort();
  const registered = registeredItems.get(clientItemId);
  if (registered) {
    registeredItems.delete(clientItemId);
    cancelRegisteredItem(registered.batchId, registered.itemId);
  }
  return true;
}

export function isTransferActive(clientItemId: string): boolean {
  return activeTransfers.has(clientItemId);
}

function fallbackId(): string {
  return `batch-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/** Stable key for `client_batch_key`, generated once per attempt. */
export function newClientKey(): string {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID();
    }
  } catch {
    // ignore
  }
  return fallbackId();
}

/**
 * Tiny semaphore. Callers acquire a slot before running and release it in a
 * `finally`; a released slot is handed directly to the next waiter.
 */
export function createSemaphore(limit: number) {
  let available = Math.max(1, Math.floor(limit));
  const waiters: Array<() => void> = [];

  async function acquire(): Promise<void> {
    if (available > 0) {
      available -= 1;
      return;
    }
    await new Promise<void>((resolve) => {
      waiters.push(resolve);
    });
  }

  function release(): void {
    const waiter = waiters.shift();
    if (waiter) {
      // Hand the slot to the waiter without incrementing `available`.
      waiter();
      return;
    }
    available += 1;
  }

  return async function run<T>(task: () => Promise<T>): Promise<T> {
    await acquire();
    try {
      return await task();
    } finally {
      release();
    }
  };
}

/** Runs `worker` over `items` with at most `limit` concurrent executions. */
export async function runWithConcurrency<T, R>(
  items: readonly T[],
  limit: number,
  worker: (item: T, index: number) => Promise<R>,
): Promise<R[]> {
  const run = createSemaphore(limit);
  return Promise.all(items.map((item, index) => run(() => worker(item, index))));
}

/**
 * Whole-document SHA-256 required by `POST /uploads/:id/complete`.
 *
 * Web Crypto has no streaming digest: `crypto.subtle.digest` needs the entire
 * buffer at once, and concatenating per-slice digests would produce a wrong
 * hash. Documents above 64 MiB therefore need a single large allocation. If
 * the browser refuses it we surface `ARQUIVO_MUITO_GRANDE_PARA_HASH_LOCAL`
 * instead of silently sending an invalid checksum.
 */
async function hashWholeFile(file: File): Promise<string> {
  try {
    // `sha256Hex` reads the Blob once; no need to copy it into another buffer.
    return await sha256Hex(file);
  } catch (cause) {
    throw new Error(
      `ARQUIVO_MUITO_GRANDE_PARA_HASH_LOCAL: não foi possível calcular o SHA-256 local de "${file.name}". ` +
        'Tente novamente em um navegador com mais memória disponível.',
      { cause },
    );
  }
}

function alreadyAccepted(status: string, conversionId: string | null): boolean {
  if (!conversionId) return false;
  return status === 'queued' || status === 'processing' || status === 'completed' || status === 'completed_with_warnings';
}

export async function startBatch(input: StartBatchInput): Promise<StartBatchResult> {
  const key = input.clientBatchKey ?? newClientKey();
  const batch = await createBatch({ client_batch_key: key });
  const failures: StartBatchFailure[] = [];
  const onItemPatch = input.onItemPatch ?? (() => undefined);

  // Registered before any worker starts, so an item still waiting for a slot
  // can be cancelled as well as one already transferring.
  const controllers = new Map<string, AbortController>();
  for (const item of input.items) {
    const controller = new AbortController();
    controllers.set(item.clientItemId, controller);
    activeTransfers.set(item.clientItemId, controller);
  }

  try {
    await runWithConcurrency(input.items, MAX_PARALLEL_TRANSFERS, async (item) => {
      const controller = controllers.get(item.clientItemId);
      if (!controller) return;
      const { signal } = controller;
      // Once cancelled, the item's state belongs to the cancel path.
      const report = (patch: TransferItemPatch) => {
        if (!signal.aborted) onItemPatch(item.clientItemId, patch);
      };

      try {
        if (signal.aborted) return;

        if (!item.file) {
          const message = 'O arquivo não está mais disponível. Selecione o documento novamente.';
          report({ state: 'needs_reselection', error: message });
          failures.push({ clientItemId: item.clientItemId, message });
          return;
        }

        report({ state: 'uploading', error: undefined });

        const registration = await registerItem(
          batch.batch_id,
          {
            client_item_id: item.clientItemId,
            filename: item.filename,
            size_bytes: item.size,
            options: item.options,
          },
          { signal },
        );
        if (signal.aborted) {
          // Cancelled while the registration was in flight: the server already
          // holds the item, so cancel it there now (best effort).
          cancelRegisteredItem(batch.batch_id, registration.item.item_id);
          return;
        }
        registeredItems.set(item.clientItemId, {
          batchId: batch.batch_id,
          itemId: registration.item.item_id,
        });

        // A replayed registration for an item already queued/processing needs no
        // bytes: the server has the document.
        if (alreadyAccepted(registration.item.status, registration.item.conversion_id)) {
          report({
            state: 'uploaded',
            uploadId: registration.upload_id,
            conversionId: registration.item.conversion_id ?? undefined,
            uploadedBytes: item.file.size,
          });
          return;
        }

        const uploadId = registration.upload_id;
        // ALWAYS the server-provided part size — never a client constant.
        const partSize = registration.part_size;
        if (!Number.isFinite(partSize) || partSize <= 0) {
          throw new Error('A API não informou um tamanho de parte válido para este envio.');
        }
        const totalParts = Math.max(1, Math.ceil(item.file.size / partSize));
        const resumeAt = Math.max(0, Math.min(registration.next_part, totalParts));

        report({
          uploadId,
          uploadedBytes: Math.min(item.file.size, resumeAt * partSize),
        });

        for (let part = resumeAt; part < totalParts; part += 1) {
          if (signal.aborted) return;
          const start = part * partSize;
          const end = Math.min(item.file.size, start + partSize);
          const chunk = item.file.slice(start, end);
          const digest = await sha256Hex(chunk);
          if (signal.aborted) return;
          await putPart(uploadId, part, chunk, digest, { signal });
          report({
            uploadedBytes: Math.min(item.file.size, end),
          });
        }

        if (signal.aborted) return;
        const wholeHash = await hashWholeFile(item.file);
        if (signal.aborted) return;
        const completed = await completeUpload(uploadId, wholeHash, totalParts, { signal });

        report({
          state: 'uploaded',
          conversionId: completed.conversion_id,
          uploadedBytes: item.file.size,
        });
      } catch (cause) {
        // An aborted request is the cancellation itself, not a failure.
        if (signal.aborted) return;
        const message = explainError(cause);
        report({ state: 'failed', error: message });
        failures.push({ clientItemId: item.clientItemId, message });
      } finally {
        if (activeTransfers.get(item.clientItemId) === controller) {
          activeTransfers.delete(item.clientItemId);
        }
        if (controllers.get(item.clientItemId) === controller) {
          registeredItems.delete(item.clientItemId);
        }
      }
    });
  } finally {
    for (const [clientItemId, controller] of controllers) {
      if (activeTransfers.get(clientItemId) === controller) activeTransfers.delete(clientItemId);
      registeredItems.delete(clientItemId);
    }
  }

  return { batch, batchId: batch.batch_id, failures };
}
