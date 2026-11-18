/**
 * Transfer worker: wrong part hash and restart recovery through `next_part`.
 */

import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { after, test } from 'node:test';

import { createHarness, outcome, waitFor, writeFixture, type Harness } from './harness.js';

const harnesses: Harness[] = [];

after(async () => {
  for (const harness of harnesses) {
    await harness.close();
  }
});

async function newHarness(options: Parameters<typeof createHarness>[0] = {}): Promise<Harness> {
  const harness = await createHarness(options);
  harnesses.push(harness);
  return harness;
}

test('a part whose hash does not match fails the item and leaves no conversion', async () => {
  const harness = await newHarness({ mock: { partSize: 8, corruptParts: 1 } });
  const file = await writeFixture(harness.root, 'broken.pdf', Buffer.from('12345'));

  const submitted = outcome(
    await harness.call('submit_files', {
      request_key: 'req-bad-hash',
      files: [{ client_item_id: 'bad', source: { type: 'local_path', path: file } }],
    }),
  );
  assert.equal(submitted.isError, false, submitted.text);
  const submissionId = String(submitted.structured.submission_id);

  const failed = await waitFor(async () => {
    const state = outcome(await harness.call('get_submission', { submission_id: submissionId }));
    const items = state.structured.items as Array<{ state: string; error?: { code: string } | null }>;
    const item = items[0];
    return item && item.state !== 'pending' && item.state !== 'uploading' ? state : null;
  }, 'the item to fail');

  const items = failed.structured.items as Array<{
    state: string;
    error: { code: string } | null;
    conversion_id: string | null;
  }>;
  assert.equal(items[0]?.state, 'failed');
  assert.equal(items[0]?.error?.code, 'UPLOAD_CHECKSUM_MISMATCH');
  assert.equal(items[0]?.conversion_id, null);

  // No conversion exists and the upload session was aborted cleanly.
  assert.equal(harness.mock.conversions.size, 0);
  const remoteItem = [...harness.mock.items.values()][0];
  assert.equal(remoteItem?.conversion_id, null);
  const upload = [...harness.mock.uploads.values()][0];
  assert.equal(upload?.state, 'aborted');
  assert.equal(upload?.parts.size, 0);
});

test('restart recovery rebuilds the ledger and resumes from the server next_part', async () => {
  const harness = await newHarness({ mock: { partSize: 8, failPartsAtOrAfter: 1 } });
  const payload = Buffer.from('0123456789abcdefghij'); // 20 bytes -> 3 parts
  const file = await writeFixture(harness.root, 'resume.pdf', payload);

  const submitted = outcome(
    await harness.call('submit_files', {
      request_key: 'req-resume',
      files: [{ client_item_id: 'resume', source: { type: 'local_path', path: file } }],
    }),
  );
  assert.equal(submitted.isError, false, submitted.text);
  const submissionId = String(submitted.structured.submission_id);

  // First run: part 0 succeeds, the mock then rejects part 1 with HTTP 500.
  await waitFor(async () => {
    const state = outcome(await harness.call('get_submission', { submission_id: submissionId }));
    const items = state.structured.items as Array<{
      state: string;
      error: { code: string; status?: number } | null;
    }>;
    return items[0]?.error?.status === 500 ? state : null;
  }, 'the transient part failure');

  assert.equal(harness.mock.partPuts.filter((entry) => entry.part_number === 0).length, 1);
  assert.equal(harness.mock.partPuts.filter((entry) => entry.part_number === 1).length, 1);
  const uploadId = [...harness.mock.uploads.keys()][0];
  assert.ok(uploadId);
  assert.equal(harness.mock.uploads.get(uploadId)?.parts.size, 1);

  // The ledger on disk must contain part 0 as done.
  const ledger = await fs.readFile(path.join(harness.configDir, 'submissions.jsonl'), 'utf8');
  assert.match(ledger, /"next_part":1/);

  // Recover: new process, same config dir and mock API, now healthy.
  harness.mock.failPartsAtOrAfter = null;
  await harness.restart();

  const finished = await waitFor(async () => {
    const state = outcome(await harness.call('get_submission', { submission_id: submissionId }));
    return state.structured.status === 'completed' ? state : null;
  }, 'the resumed submission to complete');

  const items = finished.structured.items as Array<{
    state: string;
    conversion_id: string | null;
    sha256: string;
  }>;
  assert.equal(items[0]?.state, 'completed');
  assert.ok(items[0]?.conversion_id, 'a conversion was created');
  assert.equal(items[0]?.sha256, harness.mock.conversions.get(String(items[0]?.conversion_id))?.sha256);

  // Part 0 was uploaded exactly once: the resume used next_part instead of
  // re-sending bytes that the server already confirmed.
  assert.equal(harness.mock.partPuts.filter((entry) => entry.part_number === 0).length, 1);
  assert.equal(harness.mock.partPuts.filter((entry) => entry.part_number === 1).length, 2);
  assert.equal(harness.mock.partPuts.filter((entry) => entry.part_number === 2).length, 1);
  assert.equal(harness.mock.uploads.size, 1);
  assert.equal(harness.mock.uploads.get(uploadId)?.parts.size, 3);
  assert.equal(harness.mock.conversions.size, 1);
});

test('source changed between runs fails with SOURCE_CHANGED instead of mixing parts', async () => {
  const harness = await newHarness({ mock: { partSize: 8, failPartsAtOrAfter: 1 } });
  const file = await writeFixture(harness.root, 'changing.pdf', Buffer.from('0123456789abcdef'));
  const submitted = outcome(
    await harness.call('submit_files', {
      request_key: 'req-source-changed',
      files: [{ client_item_id: 'changing', source: { type: 'local_path', path: file } }],
    }),
  );
  const submissionId = String(submitted.structured.submission_id);

  await waitFor(async () => {
    const state = outcome(await harness.call('get_submission', { submission_id: submissionId }));
    const items = state.structured.items as Array<{
      error: { code: string; status?: number } | null;
    }>;
    return items[0]?.error?.status === 500 ? state : null;
  }, 'the first part failure');

  // Same length, different bytes: only the hash can detect this.
  await fs.writeFile(file, Buffer.from('0123456789ABCDEF'));
  harness.mock.failPartsAtOrAfter = null;
  await harness.restart();

  const failed = await waitFor(async () => {
    const state = outcome(await harness.call('get_submission', { submission_id: submissionId }));
    const items = state.structured.items as Array<{ state: string; error: { code: string } | null }>;
    return items[0]?.error?.code === 'SOURCE_CHANGED' ? state : null;
  }, 'SOURCE_CHANGED');

  const items = failed.structured.items as Array<{ state: string }>;
  assert.equal(items[0]?.state, 'failed');
  assert.equal(harness.mock.conversions.size, 0);
});
