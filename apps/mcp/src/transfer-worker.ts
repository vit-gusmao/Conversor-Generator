/**
 * Background transfer worker.
 *
 * A submission is persisted before any byte moves; the MCP tool then returns
 * immediately and this worker finishes the transfer. Items are resumable: the
 * server's `next_part` is authoritative, the whole-file SHA-256 is recomputed on
 * resume, and a file that changed since it was recorded fails with
 * `SOURCE_CHANGED` instead of mixing parts from two different documents.
 */

import { createHash } from 'node:crypto';
import { createReadStream, promises as fs } from 'node:fs';
import path from 'node:path';

import { ApiClientError, sha256Buffer, type ApiClient } from './api-client.js';
import { resolveLocalPath, PathPolicyError } from './path-policy.js';
import type {
  SubmissionItemError,
  SubmissionItemRecord,
  SubmissionRecord,
  SubmissionStore,
} from './submission-store.js';

/**
 * Waits before each in-process retry of a resumable item failure: three
 * retries after the first attempt, so at most four attempts in one run.
 */
export const DEFAULT_RETRY_DELAYS_MS: readonly number[] = [2_000, 5_000, 15_000];

export interface TransferWorkerOptions {
  api: ApiClient;
  store: SubmissionStore;
  allowedRoots: readonly string[];
  concurrency: number;
  log?: (message: string) => void;
  /** Delay before each retry; the length is the number of retries. */
  retryDelaysMs?: readonly number[];
  /** Timer used for retry backoff. Injectable so tests need no real waits. */
  sleep?: (ms: number) => Promise<void>;
}

/** Streams a file through SHA-256 without buffering it. */
export async function hashFile(file: string): Promise<string> {
  const hash = createHash('sha256');
  const stream = createReadStream(file, { highWaterMark: 1024 * 1024 });
  try {
    for await (const chunk of stream) {
      hash.update(chunk as Buffer);
    }
  } finally {
    stream.destroy();
  }
  return hash.digest('hex');
}

/** Counting semaphore shared by every item of every submission. */
export class Semaphore {
  private available: number;
  private readonly waiters: Array<() => void> = [];

  constructor(size: number) {
    this.available = Math.max(1, size);
  }

  acquire(): Promise<() => void> {
    if (this.available > 0) {
      this.available -= 1;
      return Promise.resolve(this.releaseOnce());
    }
    return new Promise((resolve) => {
      this.waiters.push(() => {
        this.available -= 1;
        resolve(this.releaseOnce());
      });
    });
  }

  private releaseOnce(): () => void {
    let released = false;
    return () => {
      if (released) return;
      released = true;
      const next = this.waiters.shift();
      if (next) {
        next();
      } else {
        this.available += 1;
      }
    };
  }
}

function errorFrom(error: unknown): SubmissionItemError {
  if (error instanceof ApiClientError) {
    return {
      code: error.code,
      message: error.message,
      details: error.details,
      status: error.status,
    };
  }
  if (error instanceof PathPolicyError) {
    return { code: error.code, message: error.message, details: error.details };
  }
  if (error instanceof SourceChangedError) {
    return { code: error.code, message: error.message };
  }
  return {
    code: 'INTERNAL_ERROR',
    message: error instanceof Error ? error.message : String(error),
  };
}

/** Codes that mean the upload cannot make progress; the session is aborted. */
const NON_RESUMABLE_CODES = new Set([
  'UPLOAD_CHECKSUM_MISMATCH',
  'UPLOAD_PART_MISMATCH',
  'UPLOAD_STATE_CONFLICT',
  'UPLOAD_EXPIRED',
  'UPLOAD_NOT_FOUND',
  'FILE_TOO_LARGE',
  'INVALID_FILE',
  'INVALID_OPTION',
  'INCOMPATIBLE_OUTPUT_FORMAT',
  'BATCH_NOT_OPEN',
  'BATCH_CANCELLED',
  'SOURCE_CHANGED',
  'SOURCE_NOT_FOUND',
  'SOURCE_UNAVAILABLE',
  'NOT_A_REGULAR_FILE',
  'PATH_OUTSIDE_ROOTS',
  'UNSUPPORTED_PATH',
  'PATH_TRAVERSAL',
]);

export class TransferWorker {
  private readonly api: ApiClient;
  private readonly store: SubmissionStore;
  private readonly allowedRoots: readonly string[];
  private readonly semaphore: Semaphore;
  private readonly log: (message: string) => void;
  private readonly retryDelaysMs: readonly number[];
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly running = new Map<string, Promise<void>>();
  private readonly itemLocks = new Set<string>();

  constructor(options: TransferWorkerOptions) {
    this.api = options.api;
    this.store = options.store;
    this.allowedRoots = options.allowedRoots;
    this.semaphore = new Semaphore(options.concurrency);
    this.log = options.log ?? (() => {});
    this.retryDelaysMs = options.retryDelaysMs ?? DEFAULT_RETRY_DELAYS_MS;
    this.sleep =
      options.sleep ?? ((ms) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
  }

  /**
   * Starts (or joins) a submission. Returns as soon as the run is registered:
   * the caller never waits for bytes.
   */
  startSubmission(submissionId: string): Promise<void> {
    const active = this.running.get(submissionId);
    if (active) return active;
    const run = (async () => {
      try {
        await this.runSubmission(submissionId);
      } catch (error) {
        this.log(`submission ${submissionId} worker failed: ${(error as Error).message}`);
      } finally {
        this.running.delete(submissionId);
      }
    })();
    this.running.set(submissionId, run);
    return Promise.resolve();
  }

  /** Starts every unfinished submission (called once at startup). */
  async resumeIncomplete(): Promise<void> {
    const unfinished = this.store.listUnfinished();
    for (const record of unfinished) {
      this.log(`resuming submission ${record.submission_id}`);
      void this.startSubmission(record.submission_id);
    }
  }

  /** Resolves when no submission is being transferred. */
  async waitForIdle(): Promise<void> {
    while (this.running.size > 0) {
      await Promise.all([...this.running.values()]);
    }
  }

  /** Attempts to seal a submission's batch when nothing is in flight. */
  async sealSubmission(submissionId: string): Promise<void> {
    await this.sealIfFinished(submissionId);
  }

  /** Exposed for tests and diagnostics. */
  isRunning(submissionId: string): boolean {
    return this.running.has(submissionId);
  }

  private async runSubmission(submissionId: string): Promise<void> {
    const record = this.store.get(submissionId);
    if (!record) return;

    const work = record.items.filter(
      (item) => item.accepted && (item.state === 'pending' || item.state === 'uploading'),
    );
    if (work.length > 0) {
      await this.store.update(submissionId, (draft) => {
        draft.status = 'uploading';
      });
    }

    await Promise.all(work.map((item) => this.processItem(submissionId, item.client_item_id)));

    const latest = this.store.get(submissionId);
    if (!latest) return;

    const unfinished = latest.items.filter(
      (item) => item.accepted && (item.state === 'pending' || item.state === 'uploading'),
    );
    const failed = latest.items.filter((item) => item.state === 'failed');

    await this.store.update(submissionId, (draft) => {
      // The run is over, so nothing is in flight. Items still `pending` are
      // waiting for a later run or restart; reporting `uploading` would claim
      // a transfer that is not happening.
      if (draft.items.some((item) => item.state === 'uploading')) {
        draft.status = 'uploading';
        return;
      }
      if (draft.items.some((item) => item.state === 'pending')) {
        draft.status = 'pending';
        return;
      }
      if (draft.items.some((item) => !item.accepted) || failed.length > 0) {
        draft.status = 'completed_with_errors';
      } else {
        draft.status = 'completed';
      }
    });

    if (latest.auto_seal && unfinished.length === 0) {
      await this.sealIfFinished(submissionId);
    }
  }

  private async sealIfFinished(submissionId: string): Promise<void> {
    const record = this.store.get(submissionId);
    if (!record) return;
    // Only seal when this submission has no transfer in flight; the server
    // remains the authority on uploads from other submissions.
    if (record.items.some((item) => item.state === 'pending' || item.state === 'uploading')) {
      return;
    }
    try {
      const sealed = await this.api.sealBatch(record.batch_id);
      this.log(`batch ${record.batch_id} sealed (${sealed.status})`);
    } catch (error) {
      const mapped = errorFrom(error);
      await this.store.update(submissionId, (draft) => {
        draft.seal_error = mapped;
      });
      this.log(
        `auto-seal for batch ${record.batch_id} failed: ${mapped.code} ${mapped.message}`,
      );
    }
  }

  /**
   * Runs one item, retrying resumable failures in-process with backoff. When
   * the retries are exhausted the item stays `pending` (with its error) so the
   * next run or restart resumes it; the submission then reports `pending`.
   */
  private async processItem(submissionId: string, clientItemId: string): Promise<void> {
    const lockKey = `${submissionId}:${clientItemId}`;
    if (this.itemLocks.has(lockKey)) return;
    this.itemLocks.add(lockKey);
    try {
      for (let attempt = 0; ; attempt += 1) {
        const retryable = await this.processItemUnlocked(submissionId, clientItemId);
        if (!retryable || attempt >= this.retryDelaysMs.length) return;
        const delay = this.retryDelaysMs[attempt] ?? 0;
        this.log(
          `item ${clientItemId} retry ${attempt + 1} of ${this.retryDelaysMs.length} in ${delay} ms`,
        );
        await this.sleep(delay);
      }
    } finally {
      this.itemLocks.delete(lockKey);
    }
  }

  /** Returns true when the item failed with a resumable error and may be retried. */
  private async processItemUnlocked(submissionId: string, clientItemId: string): Promise<boolean> {
    const record = this.store.get(submissionId);
    const item = record?.items.find((entry) => entry.client_item_id === clientItemId);
    if (!record || !item || item.state === 'completed' || item.state === 'cancelled') return false;

    const patch = async (
      mutate: (draftItem: SubmissionItemRecord) => void,
    ): Promise<void> => {
      await this.store.update(submissionId, (draft) => {
        const target = draft.items.find((entry) => entry.client_item_id === clientItemId);
        if (target) mutate(target);
      });
    };

    let activeUploadId: string | null = item.upload_id;

    try {
      if (item.source.type === 'upload_id') {
        await this.attachExistingUpload(submissionId, item, patch);
        return false;
      }

      const file = await resolveLocalPath(item.source.path, this.allowedRoots);
      if (file.size !== item.size) {
        throw new SourceChangedError(
          `The file size changed since it was recorded (${item.size} -> ${file.size}).`,
        );
      }

      const sha256 = await hashFile(file.path);
      if (item.sha256 && item.sha256 !== sha256) {
        throw new SourceChangedError('The file content changed since it was recorded.');
      }

      await patch((draft) => {
        draft.sha256 = sha256;
        draft.state = 'uploading';
        draft.error = null;
        draft.filename = file.filename;
      });

      const registration = await this.api.registerItem(record.batch_id, {
        client_item_id: item.client_item_id,
        filename: file.filename,
        size_bytes: file.size,
        options: item.options,
      });

      const existing = registration.item;
      if (existing.conversion_id) {
        await patch((draft) => {
          draft.state = 'completed';
          draft.conversion_id = existing.conversion_id;
          draft.item_id = existing.item_id;
        });
        return false;
      }
      if (!registration.upload_id) {
        throw new ApiClientError(
          409,
          'UPLOAD_NOT_FOUND',
          'The server did not return an upload session for this item.',
          { item_id: existing.item_id },
        );
      }

      const uploadId = registration.upload_id;
      const partSize = registration.part_size;
      activeUploadId = uploadId;
      if (!Number.isInteger(partSize) || partSize <= 0) {
        throw new ApiClientError(0, 'INVALID_RESPONSE', 'The server returned an invalid part size.');
      }

      await patch((draft) => {
        draft.upload_id = uploadId;
        draft.item_id = existing.item_id;
        draft.next_part = registration.next_part;
      });

      const totalParts = Math.max(1, Math.ceil(file.size / partSize));
      await this.uploadParts(
        uploadId,
        file.path,
        file.size,
        partSize,
        totalParts,
        registration.next_part,
        patch,
      );

      const completed = await this.api.completeUpload(uploadId, sha256, totalParts);
      await patch((draft) => {
        draft.state = 'completed';
        draft.conversion_id = completed.conversion_id;
        draft.item_id = completed.item_id;
        draft.upload_id = uploadId;
        draft.error = null;
      });
      this.log(`item ${clientItemId} uploaded as conversion ${completed.conversion_id}`);
      return false;
    } catch (error) {
      const mapped = errorFrom(error);
      const resumable =
        !(error instanceof SourceChangedError) &&
        !(error instanceof PathPolicyError) &&
        !(error instanceof ApiClientError && NON_RESUMABLE_CODES.has(error.code));

      if (!resumable && activeUploadId) {
        // A part with the wrong bytes (or a permanently rejected document) must
        // not leave a half-completed upload that could later be mistaken for a
        // valid conversion.
        try {
          await this.api.abortUpload(activeUploadId);
        } catch {
          /* best effort */
        }
      }

      await patch((draft) => {
        draft.error = mapped;
        draft.state = resumable ? 'pending' : 'failed';
        if (!resumable) draft.upload_id = null;
      });
      this.log(`item ${clientItemId} failed: ${mapped.code} ${mapped.message}`);
      return resumable;
    }
  }

  private async attachExistingUpload(
    submissionId: string,
    item: SubmissionItemRecord,
    patch: (mutate: (draftItem: SubmissionItemRecord) => void) => Promise<void>,
  ): Promise<void> {
    if (item.source.type !== 'upload_id') return;
    const record = this.store.get(submissionId);
    if (!record) return;
    const remote = await this.api.findItemByClientId(record.batch_id, item.client_item_id);
    if (!remote || remote.upload_id !== item.source.upload_id) {
      throw new ApiClientError(
        409,
        'BATCH_ITEM_MISMATCH',
        'The upload_id does not belong to the item with this client_item_id in the batch.',
        { client_item_id: item.client_item_id, upload_id: item.source.upload_id },
      );
    }
    if (remote.conversion_id) {
      await patch((draft) => {
        draft.state = 'completed';
        draft.conversion_id = remote.conversion_id;
        draft.item_id = remote.item_id;
        draft.upload_id = remote.upload_id;
        draft.sha256 = null;
      });
      return;
    }
    // An upload that is already complete but without a conversion is not a
    // state this adapter can drive further; surface it instead of guessing.
    throw new ApiClientError(409, 'NOT_READY', 'The upload has no conversion yet.', {
      upload_id: remote.upload_id,
      status: remote.status,
    });
  }

  private async uploadParts(
    uploadId: string,
    filePath: string,
    size: number,
    partSize: number,
    totalParts: number,
    startPart: number,
    patch: (mutate: (draftItem: SubmissionItemRecord) => void) => Promise<void>,
  ): Promise<void> {
    const handle = await fs.open(path.resolve(filePath), 'r');
    try {
      for (let partNumber = Math.max(0, startPart); partNumber < totalParts; partNumber += 1) {
        const offset = partNumber * partSize;
        const expected = Math.min(partSize, size - offset);
        if (expected <= 0) {
          throw new SourceChangedError('The file is shorter than when the upload started.');
        }

        // The semaphore is held across read + upload so at most
        // `concurrency` part buffers exist at any moment.
        const release = await this.semaphore.acquire();
        let chunk: Buffer;
        try {
          const buffer = Buffer.allocUnsafe(expected);
          const { bytesRead } = await handle.read(buffer, 0, expected, offset);
          if (bytesRead !== expected) {
            throw new SourceChangedError('The file changed while it was being uploaded.');
          }
          chunk = buffer.subarray(0, bytesRead);
          const partSha = sha256Buffer(chunk);
          await this.api.putPart(uploadId, partNumber, chunk, partSha);
        } finally {
          release();
        }

        await patch((draft) => {
          draft.next_part = partNumber + 1;
        });
      }
    } finally {
      await handle.close();
    }
  }
}

export class SourceChangedError extends Error {
  readonly code = 'SOURCE_CHANGED';

  constructor(message: string) {
    super(message);
    this.name = 'SourceChangedError';
  }
}

/** Re-validates local sources at startup without hashing them yet. */
export async function revalidateLocalSources(
  store: SubmissionStore,
  allowedRoots: readonly string[],
  log: (message: string) => void = () => {},
): Promise<void> {
  for (const record of store.list()) {
    const failures: Array<{ clientItemId: string; error: SubmissionItemError }> = [];

    for (const item of record.items) {
      if (item.source.type !== 'local_path') continue;
      if (item.state !== 'pending' && item.state !== 'uploading') continue;
      try {
        const file = await resolveLocalPath(item.source.path, allowedRoots);
        if (file.size !== item.size) {
          failures.push({
            clientItemId: item.client_item_id,
            error: {
              code: 'SOURCE_CHANGED',
              message: `The file size changed since it was recorded (${item.size} -> ${file.size}).`,
            },
          });
        }
      } catch (error) {
        failures.push({ clientItemId: item.client_item_id, error: errorFrom(error) });
      }
    }

    if (failures.length === 0) continue;
    await store.update(record.submission_id, (draft) => {
      for (const failure of failures) {
        const target = draft.items.find((entry) => entry.client_item_id === failure.clientItemId);
        if (!target) continue;
        target.error = failure.error;
        target.state = 'failed';
      }
      draft.status = 'completed_with_errors';
    });
    log(`submission ${record.submission_id}: ${failures.length} source(s) failed re-validation`);
  }
}

export { type SubmissionRecord };
