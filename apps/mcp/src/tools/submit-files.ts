/**
 * `submit_files` — the only tool that starts work.
 *
 * The submission is written to the durable ledger *before* a single byte is
 * uploaded, then the transfer runs in the background and the tool returns. A
 * timed-out call therefore cannot create a second batch: repeating it with the
 * same `request_key` and arguments returns the same submission and ids.
 */

import type { McpServer } from '@modelcontextprotocol/server';
import { z } from 'zod';

import { resolveLocalPath } from '../path-policy.js';
import {
  requestKeyFingerprint,
  type SubmissionItemRecord,
  type SubmissionRecord,
} from '../submission-store.js';
import type { BatchItem } from '../types.js';
import {
  DEFAULT_PAGE_SIZE,
  fail,
  ok,
  paginate,
  submissionItemView,
  toToolError,
  type ToolContext,
} from './shared.js';

const SourceSchema = z.discriminatedUnion('type', [
  z.object({ type: z.literal('local_path'), path: z.string().min(1) }),
  z.object({ type: z.literal('upload_id'), upload_id: z.string().min(1) }),
]);

const FileSpecSchema = z.object({
  client_item_id: z.string().min(1).max(128),
  source: SourceSchema,
  options: z.record(z.string(), z.unknown()).optional(),
});

const InputSchema = z.object({
  request_key: z.string().min(1).max(128),
  files: z.array(FileSpecSchema).min(1),
  batch_id: z.string().min(1).optional(),
  auto_seal: z.boolean().optional(),
});

type Input = z.infer<typeof InputSchema>;

function itemFromRejected(
  file: Input['files'][number],
  error: { code: string; message: string; details?: Record<string, unknown>; status?: number },
): SubmissionItemRecord {
  return {
    client_item_id: file.client_item_id,
    filename: file.source.type === 'local_path' ? fileNameOf(file.source.path) : file.client_item_id,
    size: 0,
    sha256: null,
    state: 'failed',
    upload_id: null,
    conversion_id: null,
    item_id: null,
    error: {
      code: error.code,
      message: error.message,
      details: error.details ?? {},
      status: error.status,
    },
    source: file.source,
    options: file.options ?? null,
    next_part: 0,
    accepted: false,
  };
}

function fileNameOf(requested: string): string {
  const segments = requested.split(/[/\\]+/).filter((segment) => segment.length > 0);
  return segments[segments.length - 1] ?? requested;
}

async function itemFromRemote(
  remote: BatchItem | null,
  file: Input['files'][number],
): Promise<SubmissionItemRecord> {
  const uploadId = file.source.type === 'upload_id' ? file.source.upload_id : null;
  if (!remote || remote.upload_id !== uploadId) {
    return {
      ...itemFromRejected(file, {
        code: 'BATCH_ITEM_MISMATCH',
        message:
          'The upload_id does not belong to an item with this client_item_id in the batch.',
        details: { client_item_id: file.client_item_id, upload_id: uploadId },
      }),
      filename: remote?.filename ?? file.client_item_id,
    };
  }
  // An upload that already exists is never re-created: the tool just reports
  // the ids the server already assigned.
  return {
    client_item_id: file.client_item_id,
    filename: remote.filename,
    size: remote.size_bytes,
    sha256: null,
    state: 'completed',
    upload_id: remote.upload_id,
    conversion_id: remote.conversion_id,
    item_id: remote.item_id,
    error: null,
    source: file.source,
    options: file.options ?? null,
    next_part: 0,
    accepted: true,
  };
}

function summarize(record: SubmissionRecord, replayed: boolean) {
  const page = paginate(record.items, null, DEFAULT_PAGE_SIZE);
  const accepted = record.items.filter((item) => item.accepted).length;
  const rejected = record.items.length - accepted;
  return {
    submission_id: record.submission_id,
    batch_id: record.batch_id,
    status: record.status,
    accepted_count: accepted,
    rejected_count: rejected,
    next_cursor: page.next_cursor,
    replayed,
    auto_seal: record.auto_seal,
    seal_error: record.seal_error,
    items: page.items.map(submissionItemView),
  };
}

async function handle(input: Input, ctx: ToolContext) {
  const seen = new Set<string>();
  for (const file of input.files) {
    if (seen.has(file.client_item_id)) {
      return fail({
        code: 'INVALID_ARGUMENT',
        message: `Duplicate client_item_id in the request: ${file.client_item_id}`,
        details: {},
      });
    }
    seen.add(file.client_item_id);
    if (file.source.type === 'upload_id' && !input.batch_id) {
      return fail({
        code: 'INVALID_ARGUMENT',
        message: 'A file with source.type "upload_id" requires batch_id.',
        details: { client_item_id: file.client_item_id },
      });
    }
  }

  const autoSeal = input.auto_seal ?? false;
  const fingerprint = requestKeyFingerprint({
    request_key: input.request_key,
    files: input.files.map((file) => ({
      client_item_id: file.client_item_id,
      source: file.source,
      options: file.options ?? null,
    })),
    batch_id: input.batch_id ?? null,
    auto_seal: autoSeal,
  });

  const existing = ctx.store.findByRequestKey(input.request_key);
  if (existing) {
    if (existing.request_hash !== fingerprint) {
      return fail({
        code: 'IDEMPOTENT_REPLAY',
        message:
          'request_key was already used with different arguments; use a new request_key for a different submission.',
        details: { request_key: input.request_key, submission_id: existing.submission_id },
        status: 409,
      });
    }
    if (existing.items.some((item) => item.accepted && item.state !== 'completed')) {
      void ctx.worker.startSubmission(existing.submission_id);
    }
    return ok(
      summarize(existing, true),
      `Replayed submission ${existing.submission_id} for batch ${existing.batch_id} (status ${existing.status}).`,
    );
  }

  // Select or create the batch before anything is persisted. Batch creation is
  // idempotent server-side through `client_batch_key`.
  const batch = input.batch_id
    ? await ctx.api.getBatch(input.batch_id)
    : await ctx.api.createBatch(input.request_key);

  // New local uploads cannot enter a sealed/cancelled batch. upload_id sources
  // are exempt: they only reference items that already exist there.
  if (input.files.some((file) => file.source.type === 'local_path')) {
    if (batch.status === 'cancelled') {
      return fail({
        code: 'BATCH_CANCELLED',
        message: 'The batch was cancelled and does not accept new items.',
        details: { batch_id: batch.batch_id },
        status: 409,
      });
    }
    if (batch.sealed_at || batch.status !== 'accepting') {
      return fail({
        code: 'BATCH_NOT_OPEN',
        message: 'The batch no longer accepts new items; omit batch_id to create a new one.',
        details: { batch_id: batch.batch_id, status: batch.status },
        status: 409,
      });
    }
  }

  const items: SubmissionItemRecord[] = [];
  for (const file of input.files) {
    if (file.source.type === 'local_path') {
      try {
        const resolved = await resolveLocalPath(file.source.path, ctx.config.allowedRoots);
        if (resolved.size <= 0) {
          items.push(
            itemFromRejected(file, {
              code: 'INVALID_FILE',
              message: 'The file is empty.',
              details: { path: file.source.path },
            }),
          );
          continue;
        }
        items.push({
          client_item_id: file.client_item_id,
          filename: resolved.filename,
          size: resolved.size,
          sha256: null,
          state: 'pending',
          upload_id: null,
          conversion_id: null,
          item_id: null,
          error: null,
          source: file.source,
          options: file.options ?? null,
          next_part: 0,
          accepted: true,
        });
      } catch (error) {
        items.push(itemFromRejected(file, toToolError(error)));
      }
    } else {
      const remote = await ctx.api.findItemByClientId(batch.batch_id, file.client_item_id);
      items.push(await itemFromRemote(remote, file));
    }
  }

  const { record, replayed } = await ctx.store.create({
    request_key: input.request_key,
    batch_id: batch.batch_id,
    auto_seal: autoSeal,
    items,
    request_hash: fingerprint,
  });

  if (replayed) {
    return ok(
      summarize(record, true),
      `Replayed submission ${record.submission_id} for batch ${record.batch_id} (status ${record.status}).`,
    );
  }

  const hasPending = record.items.some((item) => item.accepted && item.state === 'pending');
  if (!hasPending) {
    const finalized = await ctx.store.update(record.submission_id, (draft) => {
      draft.status = draft.items.some((item) => !item.accepted)
        ? 'completed_with_errors'
        : 'completed';
    });
    if (finalized.auto_seal) {
      // Nothing to upload (e.g. only upload_id sources), but sealing still has
      // to be attempted and a 409 still has to be recorded.
      void ctx.worker.sealSubmission(finalized.submission_id);
    }
    return ok(
      summarize(finalized, false),
      `Submission ${finalized.submission_id} recorded for batch ${finalized.batch_id}; nothing to transfer.`,
    );
  }

  // Persisted first, transfer second: a timeout after this point is safe.
  void ctx.worker.startSubmission(record.submission_id);

  return ok(
    summarize(record, false),
    `Submission ${record.submission_id} accepted for batch ${record.batch_id}: ` +
      `${record.items.filter((item) => item.accepted).length} file(s) queued for upload.`,
  );
}

export function registerSubmitFiles(server: McpServer, ctx: ToolContext): void {
  server.registerTool(
    'submit_files',
    {
      title: 'Submit documents',
      description:
        'Submit PDFs or images for conversion. Persists the submission locally, starts the ' +
        'resumable upload in the background and returns immediately. Repeating the same ' +
        'request_key with identical arguments returns the same submission and ids.',
      inputSchema: InputSchema,
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true },
    },
    async (input) => {
      try {
        return await handle(input as Input, ctx);
      } catch (error) {
        return fail(error);
      }
    },
  );
}
