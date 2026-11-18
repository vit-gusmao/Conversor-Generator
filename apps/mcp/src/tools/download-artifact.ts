/**
 * `download_artifact` — streams one export to disk under the allowed roots.
 *
 * Never overwrites: an existing name becomes `name (2).ext`. The bytes are
 * hashed while streaming, the temporary file is fsynced, and only then is it
 * renamed into place. In HTTP transport mode without a destination the tool
 * returns a short-lived API reference instead of writing a file.
 */

import { createHash } from 'node:crypto';
import { randomBytes } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';

import type { McpServer } from '@modelcontextprotocol/server';
import { z } from 'zod';

import {
  PathPolicyError,
  resolveAllowedDirectory,
  safeDownloadTarget,
} from '../path-policy.js';
import { fail, ok, type ToolContext } from './shared.js';

const InputSchema = z.object({
  conversion_id: z.string().min(1),
  export_id: z.string().min(1),
  destination: z.string().min(1).optional(),
});

const REFERENCE_TTL_MS = 5 * 60 * 1000;

async function statOrNull(target: string) {
  try {
    return await fs.stat(target);
  } catch {
    return null;
  }
}

async function chooseDirectory(
  destination: string | undefined,
  ctx: ToolContext,
): Promise<{ directory: string; filename: string | null }> {
  if (destination) {
    const stat = await statOrNull(destination);
    if (stat?.isDirectory()) {
      return {
        directory: await resolveAllowedDirectory(destination, ctx.config.allowedRoots),
        filename: null,
      };
    }
    if (stat && !stat.isDirectory()) {
      const directory = await resolveAllowedDirectory(
        path.dirname(destination),
        ctx.config.allowedRoots,
      );
      return { directory, filename: path.basename(destination) };
    }
    if (/[\\/]$/.test(destination)) {
      throw new PathPolicyError(
        'DESTINATION_NOT_FOUND',
        'The destination directory does not exist.',
        { path: destination },
      );
    }
    const directory = await resolveAllowedDirectory(
      path.dirname(destination),
      ctx.config.allowedRoots,
    );
    return { directory, filename: path.basename(destination) };
  }

  const root = ctx.config.allowedRoots[0];
  if (!root) {
    throw new PathPolicyError('NO_ALLOWED_ROOT', 'No allowed root is configured.');
  }
  const directory = path.join(root, 'pdf2x-downloads');
  await fs.mkdir(directory, { recursive: true, mode: 0o700 });
  return {
    directory: await resolveAllowedDirectory(directory, ctx.config.allowedRoots),
    filename: null,
  };
}

export function registerDownloadArtifact(server: McpServer, ctx: ToolContext): void {
  server.registerTool(
    'download_artifact',
    {
      title: 'Download artifact',
      description:
        'Download one conversion export (xlsx/txt/csv) into a directory under the allowed roots ' +
        'and return {path, bytes, sha256}. Existing files are never overwritten.',
      inputSchema: InputSchema,
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false },
    },
    async (input) => {
      try {
        const exports = await ctx.api.listExports(input.conversion_id);
        const artifact = exports.items.find((item) => item.export_id === input.export_id);
        if (!artifact) {
          return fail({
            code: 'NOT_FOUND',
            message: 'Export not found for this conversion.',
            details: { conversion_id: input.conversion_id, export_id: input.export_id },
            status: 404,
          });
        }
        if (artifact.publication_status !== 'approved') {
          return fail({
            code: 'ARTIFACT_QUARANTINED',
            message: 'This artifact did not pass the audit and cannot be downloaded.',
            details: {
              export_id: artifact.export_id,
              publication_status: artifact.publication_status,
            },
            status: 409,
          });
        }

        // Remote mode: the API is reachable by the caller, so hand out a short
        // lived reference instead of writing on the server filesystem.
        if (ctx.config.transport === 'http' && !input.destination) {
          const reference = artifact.download_url ?? `/api/v1/conversions/${input.conversion_id}/exports/${input.export_id}/download`;
          return ok(
            {
              remote: true,
              conversion_id: input.conversion_id,
              export_id: artifact.export_id,
              filename: artifact.filename,
              size_bytes: artifact.size_bytes,
              sha256: artifact.sha256,
              download_url: reference,
              expires_at: new Date(Date.now() + REFERENCE_TTL_MS).toISOString(),
            },
            `Artifact ${artifact.filename} is served by the API; reference valid until ${new Date(
              Date.now() + REFERENCE_TTL_MS,
            ).toISOString()}.`,
          );
        }

        const { directory, filename } = await chooseDirectory(input.destination, ctx);
        const download = await ctx.api.downloadExport(input.conversion_id, input.export_id);
        const preferredName =
          filename ?? (artifact.filename || download.filename || `${input.export_id}.bin`);

        const finalPath = await safeDownloadTarget(directory, preferredName);
        const temporary = path.join(
          directory,
          `.${path.basename(finalPath).replace(/[^A-Za-z0-9._-]/g, '_')}.${randomBytes(6).toString('hex')}.part`,
        );

        const hash = createHash('sha256');
        let bytes = 0;
        const handle = await fs.open(temporary, 'wx', 0o600);
        try {
          for await (const chunk of download.stream) {
            const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk as Uint8Array);
            hash.update(buffer);
            bytes += buffer.length;
            await handle.write(buffer);
          }
          await handle.sync();
        } catch (error) {
          await handle.close();
          await fs.rm(temporary, { force: true });
          throw error;
        }
        await handle.close();

        const sha256 = hash.digest('hex');
        const expectedSha = artifact.sha256 ?? download.sha256 ?? null;
        if (expectedSha && expectedSha.toLowerCase() !== sha256) {
          await fs.rm(temporary, { force: true });
          return fail({
            code: 'ARTIFACT_HASH_MISMATCH',
            message: 'The downloaded artifact does not match the hash recorded by the API.',
            details: { expected: expectedSha, received: sha256, export_id: artifact.export_id },
            status: 409,
          });
        }
        if (Number.isFinite(artifact.size_bytes) && artifact.size_bytes > 0 && artifact.size_bytes !== bytes) {
          await fs.rm(temporary, { force: true });
          return fail({
            code: 'ARTIFACT_SIZE_MISMATCH',
            message: 'The downloaded artifact size does not match the metadata.',
            details: { expected: artifact.size_bytes, received: bytes, export_id: artifact.export_id },
            status: 409,
          });
        }

        await fs.rename(temporary, finalPath);

        return ok(
          {
            path: finalPath,
            bytes,
            sha256,
            filename: path.basename(finalPath),
            export_id: artifact.export_id,
            format: artifact.format,
            conversion_id: input.conversion_id,
          },
          `Saved ${path.basename(finalPath)} (${bytes} bytes) to ${directory}.`,
        );
      } catch (error) {
        return fail(error);
      }
    },
  );
}
