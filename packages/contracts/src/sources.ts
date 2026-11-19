/**
 * Source registration and import contracts (upgrade plan, docs 02/04).
 * ============================================================================
 * A source is a file or directory inside a previously granted root. The
 * engine reads only granted roots; owner identity always comes from the
 * session, never from the payload.
 */

import { z } from 'zod';
import type { ConversionOptionsV2 } from './options';
import { ContractError } from './contract-errors';

export const SOURCE_KINDS = ['file', 'directory'] as const;
export type SourceKind = (typeof SOURCE_KINDS)[number];

/** Status of one import execution (see `sources/types.py`). */
export const IMPORT_STATUSES = [
  'pending',
  'scanning',
  'submitting',
  'converting',
  'indexing',
  'completed',
  'partial',
  'failed',
  'cancelled',
] as const;
export type ImportStatus = (typeof IMPORT_STATUSES)[number];

export const IMPORT_ITEM_STATUSES = [
  'pending',
  'captured',
  'skipped',
  'uploading',
  'queued',
  'converting',
  'completed',
  'failed',
  'cancelled',
] as const;
export type ImportItemStatus = (typeof IMPORT_ITEM_STATUSES)[number];

/**
 * Root concession visible to its owner. `display_path` is shown only to that
 * owner (the engine filters by owner on every read); other owners never see
 * another person's absolute path.
 */
export interface SourceRootInfo {
  id: string;
  label: string | null;
  runtime_id: string;
  runtime_available: boolean;
  display_path: string;
  created_at: string;
}

export interface SourceCreateRequest {
  root_id: string;
  relative_path: string;
  kind: SourceKind;
  name: string;
  recursive: boolean;
  include_patterns: string[] | null;
  exclude_patterns: string[] | null;
}

export interface SourceRecord extends SourceCreateRequest {
  id: string;
  owner_id: string;
  enabled: boolean;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface SourceCandidateInfo {
  relative_path: string;
  size_bytes: number;
  mtime_ns: number;
}

export interface SourceValidation {
  source_id: string;
  ok: boolean;
  available: boolean;
  code: string | null;
  message: string | null;
  /** Bounded sample; never a guarantee of future stability. */
  sample: SourceCandidateInfo[];
  estimated_files: number;
  estimated_bytes: number;
  warnings: string[];
}

export interface SourceDefinitionSnapshot {
  source_id: string;
  root_id: string;
  root_canonical_path: string;
  relative_path: string;
  kind: SourceKind;
  recursive: boolean;
  include_patterns: string[] | null;
  exclude_patterns: string[] | null;
  version: number;
}

export interface ImportRequest {
  options: Partial<ConversionOptionsV2> | Record<string, unknown>;
  force: boolean;
  collection_id: string | null;
}

export interface ImportRecord {
  id: string;
  owner_id: string;
  source_id: string;
  source_version: number;
  status: ImportStatus;
  definition: SourceDefinitionSnapshot;
  batch_id: string | null;
  counts: {
    total: number;
    imported: number;
    skipped: number;
    failed: number;
  };
  error: { code: string; message: string } | null;
  created_at: string;
  updated_at: string;
}

const patternsSchema = z.array(z.string().min(1).max(255)).max(64).nullable();

export const sourceCreateSchema = z
  .object({
    root_id: z.string().min(1).max(128),
    relative_path: z.string().min(1).max(1024),
    kind: z.enum(SOURCE_KINDS),
    name: z.string().min(1).max(200),
    recursive: z.boolean().optional().default(false),
    include_patterns: patternsSchema.optional().default(null),
    exclude_patterns: patternsSchema.optional().default(null),
  })
  .strict();

export type SourceCreateInput = z.input<typeof sourceCreateSchema>;

/**
 * Parses `POST /sources` bodies. `owner_id` (and any other unknown field) is
 * rejected: identity comes from the session, never from the payload.
 *
 * @throws ContractError with `UNKNOWN_FIELD` or `INVALID_SOURCE`.
 */
export function parseSourceCreate(input: unknown): SourceCreateRequest {
  if (typeof input !== 'object' || input === null || Array.isArray(input)) {
    throw new ContractError('INVALID_PAYLOAD', 'O corpo deve ser um objeto JSON.');
  }
  const parsed = sourceCreateSchema.safeParse(input);
  if (!parsed.success) {
    const extra = parsed.error.issues.filter((issue) => issue.code === 'unrecognized_keys');
    if (extra.length > 0) {
      const fields = (extra[0] as { keys?: string[] }).keys ?? [];
      throw new ContractError(
        'UNKNOWN_FIELD',
        'Campo desconhecido em fonte; a identidade do dono vem da sessão.',
        { fields },
      );
    }
    const KIND_MISMATCH_CODES = new Set([
      'invalid_literal',
      'invalid_enum_value',
      'invalid_union_discriminator',
      'invalid_value',
    ]);
    const kindIssue = parsed.error.issues.find(
      (issue) => issue.path.includes('kind') && KIND_MISMATCH_CODES.has(issue.code),
    );
    if (kindIssue) {
      throw new ContractError(
        'INVALID_SOURCE',
        "kind aceita apenas 'file' (arquivo único) ou 'directory' (pasta).",
        { kind: (kindIssue as { input?: unknown }).input },
      );
    }
    throw new ContractError('INVALID_PAYLOAD', 'Cadastro de fonte inválido.', {
      issues: parsed.error.issues.map((issue) => ({
        path: issue.path.join('.'),
        message: issue.message,
      })),
    });
  }
  const data = parsed.data;
  const relative = data.relative_path.replace(/\\/g, '/');
  const parts = relative.split('/').filter((part) => part !== '' && part !== '.');
  if (relative.startsWith('/') || parts.length === 0) {
    throw new ContractError(
      'INVALID_SOURCE',
      'relative_path deve nomear um arquivo ou pasta dentro da raiz concedida.',
      { relative_path: data.relative_path },
    );
  }
  if (parts.some((part) => part === '..')) {
    throw new ContractError(
      'INVALID_SOURCE',
      "Travessia ('..') não é permitida em relative_path.",
      { relative_path: data.relative_path },
    );
  }
  return {
    root_id: data.root_id,
    relative_path: data.relative_path,
    kind: data.kind,
    name: data.name,
    recursive: data.recursive,
    include_patterns: data.include_patterns,
    exclude_patterns: data.exclude_patterns,
  };
}
