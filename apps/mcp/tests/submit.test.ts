/**
 * submit_files: idempotency, batching and per-kind option defaults.
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

async function submit(
  harness: Harness,
  requestKey: string,
  files: Array<Record<string, unknown>>,
  extra: Record<string, unknown> = {},
) {
  return outcome(await harness.call('submit_files', { request_key: requestKey, files, ...extra }));
}

async function waitCompleted(harness: Harness, submissionId: string) {
  return waitFor(async () => {
    const state = outcome(await harness.call('get_submission', { submission_id: submissionId }));
    const status = state.structured.status;
    return status === 'completed' || status === 'completed_with_errors' ? state : null;
  }, `submission ${submissionId} to finish`);
}

test('negotiates with the MCP client and exposes all tools', async () => {
  const harness = await newHarness();
  const { tools } = await harness.client.listTools();
  const names = tools.map((tool) => tool.name).sort();
  assert.deepEqual(names, [
    'cancel_batch',
    'cancel_item',
    'download_artifact',
    'get_batch',
    'get_conversion_result',
    'get_submission',
    'list_batch_items',
    'retry_item',
    'seal_batch',
    'submit_files',
  ]);
});

test('two PDFs and one image create one batch, three items and image -> txt', async () => {
  const harness = await newHarness({ mock: { partSize: 8 } });

  const first = await writeFixture(harness.root, 'invoice.pdf', Buffer.from('PDF-one-body-0001'));
  const second = await writeFixture(harness.root, 'receipt.pdf', Buffer.from('PDF-two-body-0002'));
  const image = await writeFixture(harness.root, 'scan.png', Buffer.from('PNG-image-body-0003'));

  const result = await submit(harness, 'req-three-files', [
    { client_item_id: 'doc-1', source: { type: 'local_path', path: first } },
    { client_item_id: 'doc-2', source: { type: 'local_path', path: second } },
    { client_item_id: 'img-1', source: { type: 'local_path', path: image } },
  ]);

  assert.equal(result.isError, false, result.text);
  assert.equal(result.structured.accepted_count, 3);
  assert.equal(result.structured.rejected_count, 0);

  const submissionId = String(result.structured.submission_id);
  const batchId = String(result.structured.batch_id);
  const finished = await waitCompleted(harness, submissionId);
  assert.equal(finished.structured.status, 'completed');

  assert.equal(harness.mock.batchCreates, 1);
  assert.equal(harness.mock.batches.size, 1);
  assert.equal(harness.mock.items.size, 3);

  const batch = harness.mock.batches.get(batchId);
  assert.ok(batch);
  const batchItems = [...harness.mock.items.values()].filter((item) => item.batch_id === batchId);
  assert.equal(batchItems.length, 3);

  const imageItem = batchItems.find((item) => item.filename === 'scan.png');
  assert.ok(imageItem, 'image item was registered');
  assert.deepEqual(imageItem.options.target_formats, ['txt']);
  assert.equal(imageItem.kind, 'image');

  const pdfItems = batchItems.filter((item) => item.kind === 'pdf');
  assert.equal(pdfItems.length, 2);
  for (const item of pdfItems) {
    assert.deepEqual(item.options.target_formats, ['xlsx', 'txt', 'csv']);
  }

  // One device id for every request, persisted locally, never returned.
  assert.equal(harness.mock.deviceIds.size, 1);
  const identityRaw = await fs.readFile(path.join(harness.configDir, 'identity.json'), 'utf8');
  const identity = JSON.parse(identityRaw) as { device_id: string; version: number };
  assert.equal(identity.version, 1);
  assert.match(identity.device_id, /^mcp_[0-9a-f]{32}$/);
  assert.deepEqual([...harness.mock.deviceIds], [identity.device_id]);
  assert.equal(JSON.stringify(result.structured).includes(identity.device_id), false);
});

test('same request_key and arguments replay; different arguments are rejected', async () => {
  const harness = await newHarness();
  const file = await writeFixture(harness.root, 'a.pdf', Buffer.from('AAAA'));
  const other = await writeFixture(harness.root, 'b.pdf', Buffer.from('BBBB'));

  const first = await submit(harness, 'req-replay', [
    { client_item_id: 'one', source: { type: 'local_path', path: file } },
  ]);
  assert.equal(first.isError, false, first.text);

  const replay = await submit(harness, 'req-replay', [
    { client_item_id: 'one', source: { type: 'local_path', path: file } },
  ]);
  assert.equal(replay.isError, false, replay.text);
  assert.equal(replay.structured.submission_id, first.structured.submission_id);
  assert.equal(replay.structured.batch_id, first.structured.batch_id);
  assert.equal(replay.structured.replayed, true);
  assert.equal(harness.mock.batchCreates, 1);

  const conflict = await submit(harness, 'req-replay', [
    { client_item_id: 'one', source: { type: 'local_path', path: other } },
  ]);
  assert.equal(conflict.isError, true);
  assert.equal(conflict.error?.code, 'IDEMPOTENT_REPLAY');
  assert.equal(conflict.error?.status, 409);
  assert.equal(harness.mock.batchCreates, 1);

  await waitCompleted(harness, String(first.structured.submission_id));
});

test('a second submission with the same batch_id appends items to it', async () => {
  const harness = await newHarness();
  const firstFile = await writeFixture(harness.root, 'first.pdf', Buffer.from('first-pdf'));
  const secondFile = await writeFixture(harness.root, 'second.pdf', Buffer.from('second-pdf'));
  const imageFile = await writeFixture(harness.root, 'third.png', Buffer.from('third-image'));

  const created = await submit(harness, 'req-append-1', [
    { client_item_id: 'item-a', source: { type: 'local_path', path: firstFile } },
  ]);
  assert.equal(created.isError, false, created.text);
  const batchId = String(created.structured.batch_id);

  const appended = await submit(
    harness,
    'req-append-2',
    [
      { client_item_id: 'item-b', source: { type: 'local_path', path: secondFile } },
      { client_item_id: 'item-c', source: { type: 'local_path', path: imageFile } },
    ],
    { batch_id: batchId },
  );
  assert.equal(appended.isError, false, appended.text);
  assert.equal(appended.structured.batch_id, batchId);
  assert.notEqual(appended.structured.submission_id, created.structured.submission_id);

  await waitCompleted(harness, String(created.structured.submission_id));
  await waitCompleted(harness, String(appended.structured.submission_id));

  const batchItems = [...harness.mock.items.values()].filter((item) => item.batch_id === batchId);
  assert.equal(batchItems.length, 3);
  assert.equal(harness.mock.batches.size, 1);
});

test('submit_files rejects duplicate client_item_id in one call', async () => {
  const harness = await newHarness();
  const file = await writeFixture(harness.root, 'dup.pdf', Buffer.from('dup'));
  const result = await submit(harness, 'req-dup', [
    { client_item_id: 'same', source: { type: 'local_path', path: file } },
    { client_item_id: 'same', source: { type: 'local_path', path: file } },
  ]);
  assert.equal(result.isError, true);
  assert.equal(result.error?.code, 'INVALID_ARGUMENT');
});

test('auto_seal seals the batch once this submission finished uploading', async () => {
  const harness = await newHarness();
  const file = await writeFixture(harness.root, 'sealed.pdf', Buffer.from('seal-me'));
  const submitted = await submit(
    harness,
    'req-auto-seal',
    [{ client_item_id: 'seal-1', source: { type: 'local_path', path: file } }],
    { auto_seal: true },
  );
  assert.equal(submitted.isError, false, submitted.text);
  const batchId = String(submitted.structured.batch_id);
  await waitCompleted(harness, String(submitted.structured.submission_id));

  const sealed = await waitFor(async () => {
    const batch = harness.mock.batches.get(batchId);
    return batch?.sealed_at ? batch : null;
  }, 'the batch to be sealed');
  assert.ok(sealed.sealed_at);
});

test('an upload_id source reuses the existing item and never creates a second conversion', async () => {
  const harness = await newHarness();
  const file = await writeFixture(harness.root, 'existing.pdf', Buffer.from('existing-body'));
  const first = await submit(harness, 'req-upload-id-1', [
    { client_item_id: 'existing-1', source: { type: 'local_path', path: file } },
  ]);
  assert.equal(first.isError, false, first.text);
  const batchId = String(first.structured.batch_id);
  await waitCompleted(harness, String(first.structured.submission_id));

  const remote = [...harness.mock.items.values()].find(
    (item) => item.client_item_id === 'existing-1',
  );
  assert.ok(remote?.upload_id);
  assert.ok(remote?.conversion_id);
  const conversionCount = harness.mock.conversions.size;

  const reused = await submit(
    harness,
    'req-upload-id-2',
    [
      {
        client_item_id: 'existing-1',
        source: { type: 'upload_id', upload_id: remote.upload_id },
      },
    ],
    { batch_id: batchId },
  );
  assert.equal(reused.isError, false, reused.text);
  assert.equal(reused.structured.accepted_count, 1);
  const items = reused.structured.items as Array<{ conversion_id: string | null; state: string }>;
  assert.equal(items[0]?.conversion_id, remote.conversion_id);
  assert.equal(items[0]?.state, 'completed');
  assert.equal(harness.mock.conversions.size, conversionCount);

  const mismatch = await submit(
    harness,
    'req-upload-id-3',
    [
      {
        client_item_id: 'existing-1',
        source: { type: 'upload_id', upload_id: 'upl_does_not_belong' },
      },
    ],
    { batch_id: batchId },
  );
  assert.equal(mismatch.isError, false);
  assert.equal(mismatch.structured.accepted_count, 0);
  assert.equal(mismatch.structured.rejected_count, 1);
  const rejected = mismatch.structured.items as Array<{ error: { code: string } | null }>;
  assert.equal(rejected[0]?.error?.code, 'BATCH_ITEM_MISMATCH');
});
