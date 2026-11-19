/**
 * Submission ledger durability: a torn final entry left by a crash must not
 * swallow later appends, and must not make the ledger unreadable.
 */

import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { after, before, test } from 'node:test';

import {
  SubmissionStore,
  SubmissionStoreError,
  type SubmissionItemRecord,
  type SubmissionRecord,
} from '../src/submission-store.js';

let baseDir: string;
let counter = 0;

before(async () => {
  baseDir = await fs.mkdtemp(path.join(os.tmpdir(), 'pdf2x-ledger-'));
});

after(async () => {
  await fs.rm(baseDir, { recursive: true, force: true });
});

async function freshDir(): Promise<string> {
  counter += 1;
  const dir = path.join(baseDir, `case-${counter}`);
  await fs.mkdir(dir, { recursive: true });
  return dir;
}

function item(name: string): SubmissionItemRecord {
  return {
    client_item_id: name,
    filename: `${name}.pdf`,
    size: 1,
    sha256: null,
    state: 'pending',
    upload_id: null,
    conversion_id: null,
    item_id: null,
    error: null,
    source: { type: 'local_path', path: path.join(baseDir, `${name}.pdf`) },
    options: null,
    next_part: 0,
    accepted: true,
  };
}

async function createRecord(store: SubmissionStore, requestKey: string): Promise<SubmissionRecord> {
  const result = await store.create({
    request_key: requestKey,
    batch_id: `batch-${requestKey}`,
    auto_seal: false,
    items: [item(requestKey)],
  });
  return result.record;
}

function ledgerPath(dir: string): string {
  return path.join(dir, 'submissions.jsonl');
}

test('a torn final entry is dropped on replay and the next append survives a reopen', async () => {
  const dir = await freshDir();
  const first = await SubmissionStore.open(dir);
  const kept = await createRecord(first, 'kept');
  await first.close();

  // Simulate a crash in the middle of the next entry: a prefix, no newline.
  await fs.appendFile(ledgerPath(dir), '{"v":1,"kind":"submission","record":{"submission_id":"sub_tor');

  const logs: string[] = [];
  const reopened = await SubmissionStore.open(dir, { log: (message) => logs.push(message) });
  assert.deepEqual(
    reopened.list().map((record) => record.submission_id),
    [kept.submission_id],
  );
  assert.ok(reopened.recovery && reopened.recovery.droppedBytes > 0, 'dropped bytes are reported');
  assert.equal(logs.length, 1, 'the recovery is logged once');
  assert.match(logs[0] ?? '', /dropped \d+ bytes/);

  const added = await createRecord(reopened, 'after-crash');
  await reopened.close();

  const again = await SubmissionStore.open(dir);
  assert.deepEqual(
    again.list().map((record) => record.submission_id).sort(),
    [kept.submission_id, added.submission_id].sort(),
  );
  assert.equal(again.recovery, null, 'a clean ledger reports no recovery');
  await again.close();

  const raw = await fs.readFile(ledgerPath(dir), 'utf8');
  assert.ok(raw.endsWith('\n'));
  for (const line of raw.split('\n').filter((entry) => entry.length > 0)) {
    assert.doesNotThrow(() => JSON.parse(line), 'every ledger line is a complete JSON entry');
  }
});

test('a ledger holding only a torn entry is truncated and accepts new appends', async () => {
  const dir = await freshDir();
  await fs.writeFile(ledgerPath(dir), '{"v":1,"kind":"submission","rec');

  const store = await SubmissionStore.open(dir);
  assert.equal(store.list().length, 0);
  assert.equal(await fs.readFile(ledgerPath(dir), 'utf8'), '');

  const created = await createRecord(store, 'fresh');
  await store.close();

  const reopened = await SubmissionStore.open(dir);
  assert.deepEqual(
    reopened.list().map((record) => record.submission_id),
    [created.submission_id],
  );
  await reopened.close();
});

test('a complete entry that lost only its newline is kept and separated from the next append', async () => {
  const dir = await freshDir();
  const first = await SubmissionStore.open(dir);
  const kept = await createRecord(first, 'no-newline');
  await first.close();

  const raw = await fs.readFile(ledgerPath(dir), 'utf8');
  await fs.writeFile(ledgerPath(dir), raw.slice(0, -1));

  const reopened = await SubmissionStore.open(dir);
  assert.equal(reopened.recovery, null, 'a complete entry is not dropped');
  assert.equal(reopened.list()[0]?.submission_id, kept.submission_id);
  const added = await createRecord(reopened, 'next');
  await reopened.close();

  const again = await SubmissionStore.open(dir);
  assert.deepEqual(
    again.list().map((record) => record.submission_id).sort(),
    [kept.submission_id, added.submission_id].sort(),
  );
  await again.close();
});

test('corruption in the middle of the ledger is still refused', async () => {
  const dir = await freshDir();
  const first = await SubmissionStore.open(dir);
  await createRecord(first, 'middle');
  await first.close();

  const line = (await fs.readFile(ledgerPath(dir), 'utf8')).trimEnd();
  await fs.writeFile(ledgerPath(dir), `${line}\n{not json\n${line}\n`);

  await assert.rejects(SubmissionStore.open(dir), (error: unknown) => {
    assert.ok(error instanceof SubmissionStoreError);
    assert.equal(error.code, 'LEDGER_CORRUPT');
    return true;
  });
});
