import { createHash } from 'node:crypto';

import {
  canonicalOptionsJson,
  type ConversionOptionsV2,
  type NormalizedConversionOptions,
} from '@pdf2x/contracts';

/** SHA-256 of a buffer, hex-encoded. */
export function sha256Hex(data: Buffer | string): string {
  return createHash('sha256').update(data).digest('hex');
}

/**
 * Idempotency fingerprint for a stored conversion.
 *
 * Includes the content hash *and* the canonical options, so replaying a key
 * with the same file but different output formats is a conflict instead of a
 * silent reuse of the previous conversion.
 */
export function requestFingerprint(
  contentSha256: string,
  options: NormalizedConversionOptions,
): string {
  return sha256Hex(`${contentSha256}\u0000${canonicalOptionsJson(options)}`);
}

/**
 * A stable, opaque cursor for list endpoints.
 * Encodes the sort position so a page boundary survives new inserts.
 */
export function encodeCursor(parts: Record<string, string | number | null>): string {
  const json = JSON.stringify(parts);
  return Buffer.from(json, 'utf8').toString('base64url');
}

/** Returns `null` for a malformed cursor instead of throwing. */
export function decodeCursor(cursor: string | null | undefined): Record<string, unknown> | null {
  if (!cursor) return null;
  try {
    const parsed: unknown = JSON.parse(Buffer.from(cursor, 'base64url').toString('utf8'));
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) return null;
    return parsed as Record<string, unknown>;
  } catch {
    return null;
  }
}

export function cursorString(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null;
}

export function cursorInt(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}
