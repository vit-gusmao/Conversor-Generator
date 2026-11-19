/**
 * In-process retry of resumable transfer failures. A fake API and an injected
 * sleep let the backoff schedule be asserted without real waits.
 */

import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { after, before, test } from 'node:test';

import { ApiClientError, type ApiClient } from '../src/api-client.js';
import {
  SubmissionStore,
  type SubmissionItemRecord,
} from '../src/submission-store.js';
import { DEFAULT_RETRY_DELAYS_MS, TransferWorker } from '../src/transfer-worker.js';

let baseDir: string;
let root: string;
let counter = 0;

before(async () => {
  baseDir = await fs.mkdtemp(path.join(os.tmpdir(), 'pdf2x-retry-'));
  root = path.join(baseDir, 'root');
  await fs.mkdir(root, { recursive: true });
});

after(async () => {
  await fs.rm(baseDir, { recursive: true, force: true });
});

interface Failure {
  remaining: number;
  code: string;
  status: number;
}

/** Two 4-byte parts. `putPart` fails `failure.remaining` times, then succeeds. */
class FakeApi {
  readonly parts = new Set<number>();
  putCalls = 0;
  completed = false;

  constructor(private readonly failure: Failure) {}

  async registerItem(): Promise<unknown> {
    let nextPart = 0;
    while (this.parts.has(nextPart)) nextPart += 1;
    return {
      item: { item_id: 'item-1', conversion_id: null },
      upload_id: 'upload-1',
      part_size: 4,
      next_part: nextPart,
      replayed: false,
    };
  }

  async putPart(_uploadId: string, partNumber: number): Promise<unknown> {
    this.putCalls += 1;
    if (this.failure.remaining > 0) {
      this.failure.remaining -= 1;
      throw new ApiClientError(this.failure.status, this.failure.code, 'simulated failure');
    }
    this.parts.add(partNumber);
    return {};
  }

  async completeUpload(): Promise<unknown> {
    this.completed = true;
    return {
      upload_id: 'upload-1',
      item_id: 'item-1',
      conversion_id: 'conv-1',
      status: 'queued',
      message: '',
      replayed: false,
    };
  }

  async abortUpload(): Promise<unknown> {
    return {};
  }
}

async function setup(failure: Failure) {
  counter += 1;
  const dir = path.join(baseDir, `case-${counter}`);
  await fs.mkdir(dir, { recursive: true });
  const file = path.join(root, `doc-${counter}.pdf`);
  await fs.writeFile(file, Buffer.from('01234567'));

  const store = await SubmissionStore.open(dir);
  const itemRecord: SubmissionItemRecord = {
    client_item_id: 'doc',
    filename: path.basename(file),
    size: 8,
    sha256: null,
    state: 'pending',
    upload_id: null,
    conversion_id: null,
    item_id: null,
    error: null,
    source: { type: 'local_path', path: file },
    options: null,
    next_part: 0,
    accepted: true,
  };
  const { record } = await store.create({
    request_key: `retry-${counter}`,
    batch_id: 'batch-1',
    auto_seal: false,
    items: [itemRecord],
  });

  const api = new FakeApi(failure);
  const delays: number[] = [];
  const worker = new TransferWorker({
    api: api as unknown as ApiClient,
    store,
    allowedRoots: [root],
    concurrency: 2,
    sleep: async (ms) => {
      delays.push(ms);
    },
  });
  return { store, worker, api, delays, submissionId: record.submission_id };
}

test('the production backoff is 2s, 5s and 15s', () => {
  assert.deepEqual([...DEFAULT_RETRY_DELAYS_MS], [2_000, 5_000, 15_000]);
});

test('transient part failures are retried in-process and the item completes', async () => {
  const { store, worker, api, delays, submissionId } = await setup({
    remaining: 2,
    code: 'SERVICE_UNAVAILABLE',
    status: 503,
  });

  await worker.startSubmission(submissionId);
  await worker.waitForIdle();

  assert.deepEqual(delays, [2_000, 5_000]);
  assert.equal(api.putCalls, 4, 'two failed puts, then part 0 and part 1');
  assert.equal(api.completed, true);
  const record = store.get(submissionId);
  assert.equal(record?.items[0]?.state, 'completed');
  assert.equal(record?.items[0]?.conversion_id, 'conv-1');
  assert.equal(record?.items[0]?.error, null);
  assert.equal(record?.status, 'completed');
  await store.close();
});

test('after the retry budget is spent the item stays pending and the submission reports pending', async () => {
  const { store, worker, api, delays, submissionId } = await setup({
    remaining: 1_000,
    code: 'SERVICE_UNAVAILABLE',
    status: 503,
  });

  await worker.startSubmission(submissionId);
  await worker.waitForIdle();

  // One attempt plus three retries.
  assert.deepEqual(delays, [2_000, 5_000, 15_000]);
  assert.equal(api.putCalls, 4);
  assert.equal(api.completed, false);

  const record = store.get(submissionId);
  assert.equal(record?.items[0]?.state, 'pending', 'the item waits for a later resume');
  assert.equal(record?.items[0]?.error?.code, 'SERVICE_UNAVAILABLE');
  assert.equal(record?.status, 'pending', 'no transfer is in flight, so not "uploading"');
  assert.equal(worker.isRunning(submissionId), false);
  assert.ok(
    store.listUnfinished().some((entry) => entry.submission_id === submissionId),
    'a restart still picks the submission up',
  );
  await store.close();
});

test('a non-resumable rejection is not retried', async () => {
  const { store, worker, api, delays, submissionId } = await setup({
    remaining: 1_000,
    code: 'UPLOAD_CHECKSUM_MISMATCH',
    status: 409,
  });

  await worker.startSubmission(submissionId);
  await worker.waitForIdle();

  assert.deepEqual(delays, []);
  assert.equal(api.putCalls, 1);
  const record = store.get(submissionId);
  assert.equal(record?.items[0]?.state, 'failed');
  assert.equal(record?.items[0]?.error?.code, 'UPLOAD_CHECKSUM_MISMATCH');
  assert.equal(record?.status, 'completed_with_errors');
  await store.close();
});
