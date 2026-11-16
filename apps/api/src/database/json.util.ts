/**
 * JSON columns are stored as TEXT (sqlite) / text (postgres) so the same code
 * runs on both providers. These helpers keep the casts in one place.
 */

export function parseJson<T>(value: string | null | undefined): T | null {
  if (!value) return null;
  try {
    return JSON.parse(value) as T;
  } catch {
    return null;
  }
}

export function stringifyJson(value: unknown): string | null {
  if (value === undefined || value === null) return null;
  try {
    return JSON.stringify(value);
  } catch {
    return null;
  }
}
