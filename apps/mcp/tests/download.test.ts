/**
 * download_artifact: streams under the allowed roots and never overwrites.
 */

import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';

import { createHarness, outcome, sha256, waitFor, writeFixture, type Harness } from './harness.js';

interface DownloadFixture {
  harness: Harness;
  root: string;
  conversionId: string;
  exportId: string;
  artifact: { filename: string; sha256: string; size_bytes: number };
}

async function completedConversion(): Promise<DownloadFixture> {
  const harness = await createHarness();
  const file = await writeFixture(harness.root, 'report.pdf', Buffer.from('PDF-report-body'));
  const submitted = outcome(
    await harness.call('submit_files', {
      request_key: 'req-download',
      files: [{ client_item_id: 'report', source: { type: 'local_path', path: file } }],
    }),
  );
  assert.equal(submitted.isError, false, submitted.text);
  const submissionId = String(submitted.structured.submission_id);

  const completed = await waitFor(async () => {
    const state = outcome(await harness.call('get_submission', { submission_id: submissionId }));
    const items = state.structured.items as Array<{ conversion_id: string | null }>;
    return state.structured.status === 'completed' && items[0]?.conversion_id ? state : null;
  }, 'the conversion to finish');
  const conversionId = String(
    (completed.structured.items as Array<{ conversion_id: string }>)[0]?.conversion_id,
  );

  const result = outcome(await harness.call('get_conversion_result', { conversion_id: conversionId }));
  assert.equal(result.isError, false, result.text);
  const artifacts = result.structured.artifacts as Array<{
    export_id: string;
    format: string;
    filename: string;
    sha256: string;
    size_bytes: number;
  }>;
  assert.equal(artifacts.length, 3, 'xlsx, txt and csv exports');
  assert.equal((result.structured.preview as { rows: unknown[] }).rows.length, 2);

  const artifact = artifacts.find((entry) => entry.format === 'csv') ?? artifacts[0]!;
  return {
    harness,
    root: await fs.realpath(harness.root),
    conversionId,
    exportId: artifact.export_id,
    artifact,
  };
}

test('download_artifact writes inside the root, then appends " (2)" instead of overwriting', async () => {
  const fixture = await completedConversion();
  const { harness, conversionId, exportId, artifact } = fixture;
  try {
    const destination = path.join(fixture.root, 'out.csv');
    const first = outcome(
      await harness.call('download_artifact', {
        conversion_id: conversionId,
        export_id: exportId,
        destination,
      }),
    );
    assert.equal(first.isError, false, first.text);
    assert.equal(first.structured.path, destination);
    assert.equal(first.structured.bytes, artifact.size_bytes);
    assert.equal(first.structured.sha256, artifact.sha256);

    const firstBytes = await fs.readFile(destination);
    assert.equal(sha256(firstBytes), artifact.sha256);

    const second = outcome(
      await harness.call('download_artifact', {
        conversion_id: conversionId,
        export_id: exportId,
        destination,
      }),
    );
    assert.equal(second.isError, false, second.text);
    assert.notEqual(second.structured.path, destination);
    assert.equal(path.basename(String(second.structured.path)), 'out (2).csv');
    assert.equal(await fs.readFile(destination).then((data) => sha256(data)), artifact.sha256);
  } finally {
    await harness.close();
  }
});

test('download_artifact refuses destinations outside the allowed roots', async () => {
  const fixture = await completedConversion();
  const { harness, conversionId, exportId } = fixture;
  try {
    const outside = path.join(harness.outsideDir, 'escape.csv');
    const result = outcome(
      await harness.call('download_artifact', {
        conversion_id: conversionId,
        export_id: exportId,
        destination: outside,
      }),
    );
    assert.equal(result.isError, true);
    assert.equal(result.error?.code, 'PATH_OUTSIDE_ROOTS');
    await assert.rejects(fs.stat(outside));
  } finally {
    await harness.close();
  }
});

test('download_artifact writes the artifact filename when given a directory', async () => {
  const fixture = await completedConversion();
  const { harness, conversionId, exportId, artifact } = fixture;
  try {
    const result = outcome(
      await harness.call('download_artifact', {
        conversion_id: conversionId,
        export_id: exportId,
        destination: fixture.root,
      }),
    );
    assert.equal(result.isError, false, result.text);
    assert.equal(path.basename(String(result.structured.path)), artifact.filename);
    assert.equal(result.structured.sha256, artifact.sha256);
  } finally {
    await harness.close();
  }
});
