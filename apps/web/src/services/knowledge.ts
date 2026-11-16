import { apiFetch } from '@/lib/api';

export interface KnowledgeCollection {
  collection_id: string;
  name: string;
  description: string | null;
  scope: 'documents' | 'studies' | string;
  chunks: number;
  created_at?: string | null;
}

export interface IndexJob {
  job_id: string;
  status: string;
  collection_id: string | null;
  source: Record<string, unknown>;
  stats: Record<string, unknown>;
  error: { code: string; message: string } | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export async function listCollections(): Promise<{ items: KnowledgeCollection[]; total: number }> {
  return apiFetch<{ items: KnowledgeCollection[]; total: number }>('/knowledge/collections');
}

export async function createCollection(input: {
  name: string;
  description?: string | null;
  scope?: 'documents' | 'studies';
}): Promise<KnowledgeCollection> {
  return apiFetch<KnowledgeCollection>('/knowledge/collections', { method: 'POST', json: input });
}

export async function deleteCollection(id: string): Promise<void> {
  await apiFetch<void>(`/knowledge/collections/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

export async function createIndexJob(input: {
  collection_id?: string | null;
  collection_name?: string | null;
  conversion_ids?: string[];
  source_paths?: string[];
  scope?: 'documents' | 'studies';
}): Promise<IndexJob> {
  return apiFetch<IndexJob>('/knowledge/index-jobs', { method: 'POST', json: input });
}

export async function getIndexJob(id: string): Promise<IndexJob> {
  return apiFetch<IndexJob>(`/knowledge/index-jobs/${encodeURIComponent(id)}`);
}
