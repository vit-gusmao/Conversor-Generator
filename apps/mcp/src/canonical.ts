/**
 * Deterministic serialization used for idempotency fingerprints.
 *
 * Two calls that mean the same thing must hash the same, regardless of property
 * order or object identity; two calls that differ in any argument that affects
 * the created resources must hash differently.
 */

import { createHash } from 'node:crypto';

export function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== 'object') {
    return JSON.stringify(value) ?? 'null';
  }
  if (Array.isArray(value)) {
    return `[${value.map((entry) => canonicalJson(entry)).join(',')}]`;
  }
  const record = value as Record<string, unknown>;
  const keys = Object.keys(record)
    .filter((key) => record[key] !== undefined)
    .sort();
  return `{${keys.map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(',')}}`;
}

export function sha256Hex(value: string | Buffer): string {
  return createHash('sha256').update(value).digest('hex');
}

export function fingerprint(value: unknown): string {
  return sha256Hex(canonicalJson(value));
}
