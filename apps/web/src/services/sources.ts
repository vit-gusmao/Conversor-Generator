/** Typed wrappers for the source/import endpoints (`/api/v1/sources`). */

import type {
  ImportRecord,
  SourceCandidateInfo,
  SourceRootInfo,
  SourceRecord,
  SourceValidation,
} from '@pdf2x/contracts';
import { apiFetch, buildUrl } from '@/lib/api';

export interface CreateSourceInput {
  root_id: string;
  relative_path: string;
  kind: 'file' | 'directory';
  name: string;
  recursive?: boolean;
  include_patterns?: string[] | null;
  exclude_patterns?: string[] | null;
}

export interface ImportItemInfo {
  id: number;
  relative_path: string;
  status: string;
  size_bytes: number;
  sha256: string | null;
  upload_id: string | null;
  conversion_id: string | null;
  error_code: string | null;
  error_message: string | null;
  attempts: number;
}

export interface StartImportInput {
  options?: Record<string, unknown>;
  force?: boolean;
  collection_id?: string | null;
}

export async function listSourceRoots(): Promise<SourceRootInfo[]> {
  const payload = await apiFetch<{ items: SourceRootInfo[] }>('/source-roots');
  return payload.items ?? [];
}

export async function listSources(): Promise<SourceRecord[]> {
  const payload = await apiFetch<{ items: SourceRecord[] }>('/sources');
  return payload.items ?? [];
}

export async function createSource(input: CreateSourceInput): Promise<SourceRecord> {
  return apiFetch<SourceRecord>('/sources', { method: 'POST', json: input });
}

export async function updateSource(
  sourceId: string,
  version: number,
  changes: Partial<CreateSourceInput> & { enabled?: boolean },
): Promise<SourceRecord> {
  return apiFetch<SourceRecord>(`/sources/${encodeURIComponent(sourceId)}`, {
    method: 'PATCH',
    json: { ...changes, version },
  });
}

export async function archiveSource(sourceId: string): Promise<{ archived: boolean }> {
  return apiFetch<{ archived: boolean }>(`/sources/${encodeURIComponent(sourceId)}`, {
    method: 'DELETE',
  });
}

export async function validateSource(sourceId: string): Promise<SourceValidation> {
  return apiFetch<SourceValidation>(`/sources/${encodeURIComponent(sourceId)}/validate`, {
    method: 'POST',
  });
}

export async function startImport(
  sourceId: string,
  input: StartImportInput,
  idempotencyKey: string,
): Promise<ImportRecord> {
  return apiFetch<ImportRecord>(`/sources/${encodeURIComponent(sourceId)}/imports`, {
    method: 'POST',
    json: input,
    headers: { 'Idempotency-Key': idempotencyKey },
  });
}

export async function getImport(importId: string): Promise<ImportRecord> {
  return apiFetch<ImportRecord>(`/imports/${encodeURIComponent(importId)}`);
}

export async function listImportItems(importId: string): Promise<ImportItemInfo[]> {
  const payload = await apiFetch<{ items: ImportItemInfo[] }>(
    `/imports/${encodeURIComponent(importId)}/items`,
  );
  return payload.items ?? [];
}

export async function cancelImport(importId: string): Promise<{ cancel_requested: boolean }> {
  return apiFetch<{ cancel_requested: boolean }>(
    `/imports/${encodeURIComponent(importId)}/cancel`,
    { method: 'POST' },
  );
}

export function importUrl(importId: string): string {
  return buildUrl(`/imports/${encodeURIComponent(importId)}`);
}

export type { ImportRecord, SourceCandidateInfo, SourceRootInfo, SourceRecord, SourceValidation };
