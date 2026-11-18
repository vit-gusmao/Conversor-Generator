/**
 * Durable local ledger of submissions.
 *
 * `submissions.jsonl` is append-only: every state transition appends a full
 * snapshot and fsyncs before the call returns, so a crash or an MCP timeout
 * cannot lose the fact that an upload was started. Compaction/update rewrites
 * the file atomically via a temp file + rename.
 *
 * Replays are guarded by `request_key`: the canonical arguments are hashed and
 * stored, and a replay with different arguments is refused instead of silently
 * creating a second batch.
 */

import { randomBytes } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';

import { fingerprint } from './canonical.js';

export type SubmissionState =
  | 'pending'
  | 'uploading'
  | 'completed'
  | 'completed_with_errors'
  | 'failed';

export type SubmissionItemState = 'pending' | 'uploading' | 'completed' | 'failed' | 'cancelled';

export type FileSource =
  | { type: 'local_path'; path: string }
  | { type: 'upload_id'; upload_id: string };

export interface SubmissionItemError {
  code: string;
  message: string;
  details?: Record<string, unknown>;
  status?: number;
}

export interface SubmissionItemRecord {
  client_item_id: string;
  filename: string;
  size: number;
  sha256: string | null;
  state: SubmissionItemState;
  upload_id: string | null;
  conversion_id: string | null;
  item_id: string | null;
  error: SubmissionItemError | null;
  source: FileSource;
  options: Record<string, unknown> | null;
  /** Server-confirmed resume point (0-based part number). */
  next_part: number;
  /** False when the file was rejected before any upload was attempted. */
  accepted: boolean;
}

export interface SubmissionRecord {
  submission_id: string;
  request_key: string;
  request_hash: string;
  batch_id: string;
  auto_seal: boolean;
  status: SubmissionState;
  items: SubmissionItemRecord[];
  seal_error: SubmissionItemError | null;
  created_at: string;
  updated_at: string;
}

export interface NewSubmission {
  request_key: string;
  batch_id: string;
  auto_seal: boolean;
  items: SubmissionItemRecord[];
  /**
   * Fingerprint of the *request arguments*. When the caller created a batch,
   * `batch_id` is already resolved, so the hash must not be recomputed from the
   * resolved record or a replay would look different.
   */
  request_hash?: string;
}

export interface CreateSubmissionResult {
  record: SubmissionRecord;
  replayed: boolean;
}

interface LedgerLine {
  v: 1;
  kind: 'submission';
  record: SubmissionRecord;
}

export class SubmissionStoreError extends Error {
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(code: string, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.name = 'SubmissionStoreError';
    this.code = code;
    this.details = details;
  }
}

const LEDGER_FILE = 'submissions.jsonl';

function nowIso(): string {
  return new Date().toISOString();
}

function newSubmissionId(): string {
  return `sub_${randomBytes(12).toString('hex')}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * Fingerprints the arguments that affect which resources a call creates.
 * `batch_id`, `auto_seal` and the ordered file specs are all significant.
 */
export function requestKeyFingerprint(input: {
  request_key: string;
  files: Array<{
    client_item_id: string;
    source: FileSource;
    options?: Record<string, unknown> | null;
  }>;
  batch_id?: string | null;
  auto_seal?: boolean;
}): string {
  return fingerprint({
    request_key: input.request_key,
    batch_id: input.batch_id ?? null,
    auto_seal: input.auto_seal ?? false,
    files: input.files.map((file) => ({
      client_item_id: file.client_item_id,
      source:
        file.source.type === 'local_path'
          ? { type: 'local_path', path: file.source.path }
          : { type: 'upload_id', upload_id: file.source.upload_id },
      options: file.options ?? null,
    })),
  });
}

export class SubmissionStore {
  readonly directory: string;
  readonly file: string;
  private readonly records = new Map<string, SubmissionRecord>();
  private writeQueue: Promise<unknown> = Promise.resolve();
  private closed = false;

  private constructor(directory: string) {
    this.directory = directory;
    this.file = path.join(directory, LEDGER_FILE);
  }

  /** Opens (and replays) the ledger in `${configDir}`. */
  static async open(configDir: string): Promise<SubmissionStore> {
    const store = new SubmissionStore(configDir);
    await fs.mkdir(configDir, { recursive: true, mode: 0o700 });
    await store.replay();
    return store;
  }

  private async replay(): Promise<void> {
    let raw: string;
    try {
      raw = await fs.readFile(this.file, 'utf8');
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return;
      throw error;
    }
    const lines = raw.split('\n');
    for (let index = 0; index < lines.length; index += 1) {
      const line = lines[index]?.trim();
      if (!line) continue;
      let parsed: unknown;
      try {
        parsed = JSON.parse(line);
      } catch {
        // A crash can leave one truncated trailing line; anything else is
        // corruption and must not be silently discarded.
        const isLast = lines.slice(index + 1).every((rest) => rest.trim() === '');
        if (isLast) break;
        throw new SubmissionStoreError(
          'LEDGER_CORRUPT',
          `The submission ledger is corrupt at line ${index + 1}: ${this.file}`,
        );
      }
      if (!isRecord(parsed) || parsed.v !== 1 || parsed.kind !== 'submission') {
        throw new SubmissionStoreError(
          'LEDGER_CORRUPT',
          `The submission ledger contains an unknown entry at line ${index + 1}: ${this.file}`,
        );
      }
      const record = parsed.record as SubmissionRecord | undefined;
      if (!isRecord(record) || typeof record.submission_id !== 'string') {
        throw new SubmissionStoreError(
          'LEDGER_CORRUPT',
          `The submission ledger contains an invalid record at line ${index + 1}: ${this.file}`,
        );
      }
      this.records.set(record.submission_id, record);
    }
  }

  private assertOpen(): void {
    if (this.closed) {
      throw new SubmissionStoreError('STORE_CLOSED', 'The submission store is closed.');
    }
  }

  list(): SubmissionRecord[] {
    return [...this.records.values()].sort((a, b) => a.created_at.localeCompare(b.created_at));
  }

  listUnfinished(): SubmissionRecord[] {
    return this.list().filter(
      (record) =>
        record.status === 'pending' ||
        record.status === 'uploading' ||
        record.items.some((item) => item.state === 'pending' || item.state === 'uploading'),
    );
  }

  get(submissionId: string): SubmissionRecord | undefined {
    const record = this.records.get(submissionId);
    return record ? structuredClone(record) : undefined;
  }

  findByRequestKey(requestKey: string): SubmissionRecord | undefined {
    for (const record of this.records.values()) {
      if (record.request_key === requestKey) return structuredClone(record);
    }
    return undefined;
  }

  /**
   * Creates a submission, or replays an identical one.
   *
   * @throws SubmissionStoreError `IDEMPOTENT_REPLAY` when `request_key` was
   *         used with different arguments.
   */
  async create(input: NewSubmission): Promise<CreateSubmissionResult> {
    this.assertOpen();
    const record = buildSubmissionRecord(input);
    return this.withWriteLock(async () => {
      const existing = this.findByRequestKey(input.request_key);
      if (existing) {
        if (existing.request_hash !== record.request_hash) {
          throw new SubmissionStoreError(
            'IDEMPOTENT_REPLAY',
            'request_key was already used with different arguments.',
            {
              request_key: input.request_key,
              submission_id: existing.submission_id,
            },
          );
        }
        return { record: existing, replayed: true };
      }
      await this.appendUnlocked(record);
      return { record: structuredClone(record), replayed: false };
    });
  }

  /** Appends one snapshot and fsyncs. */
  async append(record: SubmissionRecord): Promise<void> {
    this.assertOpen();
    await this.withWriteLock(() => this.appendUnlocked(record));
  }

  private async appendUnlocked(record: SubmissionRecord): Promise<void> {
    const line: LedgerLine = { v: 1, kind: 'submission', record };
    const payload = `${JSON.stringify(line)}\n`;
    const handle = await fs.open(this.file, 'a', 0o600);
    try {
      await handle.writeFile(payload, 'utf8');
      await handle.sync();
    } finally {
      await handle.close();
    }
    this.records.set(record.submission_id, structuredClone(record));
  }

  /** Applies a mutation atomically and persists the new snapshot. */
  async update(
    submissionId: string,
    mutate: (draft: SubmissionRecord) => void,
  ): Promise<SubmissionRecord> {
    this.assertOpen();
    return this.withWriteLock(async () => {
      const current = this.records.get(submissionId);
      if (!current) {
        throw new SubmissionStoreError('NOT_FOUND', `Unknown submission: ${submissionId}`, {
          submission_id: submissionId,
        });
      }
      const draft = structuredClone(current);
      mutate(draft);
      draft.updated_at = nowIso();
      await this.appendUnlocked(draft);
      return structuredClone(draft);
    });
  }

  /** Atomic rewrite (temp file + rename) used for maintenance operations. */
  async rewriteAll(): Promise<void> {
    this.assertOpen();
    await this.withWriteLock(async () => {
      const temporary = path.join(
        this.directory,
        `.${LEDGER_FILE}.${process.pid}.${randomBytes(4).toString('hex')}.tmp`,
      );
      const handle = await fs.open(temporary, 'w', 0o600);
      try {
        for (const record of this.list()) {
          const line: LedgerLine = { v: 1, kind: 'submission', record };
          await handle.writeFile(`${JSON.stringify(line)}\n`, 'utf8');
        }
        await handle.sync();
      } finally {
        await handle.close();
      }
      try {
        await fs.rename(temporary, this.file);
      } catch (error) {
        await fs.rm(temporary, { force: true });
        throw error;
      }
      // Directory fsync keeps the rename durable where the platform allows it.
      try {
        const dirHandle = await fs.open(this.directory, 'r');
        try {
          await dirHandle.sync();
        } finally {
          await dirHandle.close();
        }
      } catch {
        /* Windows does not permit fsync on a directory handle. */
      }
    });
  }

  async close(): Promise<void> {
    await this.writeQueue.catch(() => undefined);
    this.closed = true;
  }

  private withWriteLock<T>(operation: () => Promise<T>): Promise<T> {
    const run = this.writeQueue.then(operation, operation);
    this.writeQueue = run.catch(() => undefined);
    return run;
  }
}

/** Builds the in-memory shape for a brand new submission. */
export function buildSubmissionRecord(input: NewSubmission): SubmissionRecord {
  const timestamp = nowIso();
  return {
    submission_id: newSubmissionId(),
    request_key: input.request_key,
    request_hash:
      input.request_hash ??
      requestKeyFingerprint({
        request_key: input.request_key,
        files: input.items.map((item) => ({
          client_item_id: item.client_item_id,
          source: item.source,
          options: item.options ?? null,
        })),
        batch_id: input.batch_id,
        auto_seal: input.auto_seal,
      }),
    batch_id: input.batch_id,
    auto_seal: input.auto_seal,
    status: 'pending',
    items: structuredClone(input.items),
    seal_error: null,
    created_at: timestamp,
    updated_at: timestamp,
  };
}
