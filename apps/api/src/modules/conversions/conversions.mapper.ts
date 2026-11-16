import type {
  Conversion,
  ConversionListItem,
  ConversionStatus,
  DocumentInfo,
  RunStats,
  ValidationReport,
} from '@pdf2x/contracts';

import {
  asConversionStatus,
  asQualityState,
  isActive,
  stageKeyForStatus,
} from '../../common/status';
import { parseJson } from '../../database/json.util';
import type { FileInspection } from '../../upload/file-validation.service';

export interface DocumentRowShape {
  id: string;
  filename: string;
  mimeType: string;
  sizeBytes: number;
  sha256: string;
  storageKey: string;
  pageCount: number | null;
  ownerId: string;
  createdAt: Date;
}

export interface RunRowShape {
  id: string;
  conversionId: string;
  attempt: number;
  status: string;
  progress: number;
  stage: string;
  startedAt: Date | null;
  finishedAt: Date | null;
  engineVersion: string | null;
  configJson: string | null;
  errorCode: string | null;
  errorMessage: string | null;
  statsJson: string | null;
  createdAt: Date;
}

export interface ConversionRowShape {
  id: string;
  documentId: string;
  ownerId: string;
  status: string;
  quality: string;
  progress: number;
  stage: string;
  activeRunId: string | null;
  errorCode: string | null;
  errorMessage: string | null;
  statsJson: string | null;
  validationJson: string | null;
  deletedAt: Date | null;
  createdAt: Date;
  updatedAt: Date;
}

export interface ConversionWithRelations extends ConversionRowShape {
  document: DocumentRowShape;
  runs: RunRowShape[];
}

export function linksFor(conversionId: string): Record<string, string> {
  const base = `/api/v1/conversions/${conversionId}`;
  return {
    self: base,
    status: base,
    events: `${base}/events`,
    result: `${base}/result`,
    preview: `${base}/preview`,
    download: `${base}/download`,
    audit: `${base}/audit`,
  };
}

export function toDocumentInfo(document: DocumentRowShape): DocumentInfo {
  return {
    id: document.id,
    filename: document.filename,
    mime_type: document.mimeType,
    size_bytes: document.sizeBytes,
    sha256: document.sha256,
    page_count: document.pageCount,
    created_at: document.createdAt.toISOString(),
  };
}

export function toConversionDetail(row: ConversionWithRelations): Conversion {
  const status = asConversionStatus(row.status) ?? 'received';
  const stats = parseJson<RunStats>(row.statsJson);
  const validation = parseJson<ValidationReport>(row.validationJson);
  const run = row.runs[0] ?? null;
  const runId = row.activeRunId ?? run?.id ?? '';

  return {
    conversion_id: row.id,
    status,
    stage: stageKeyForStatus(status),
    progress: row.progress,
    progress_indeterminate: isActive(status) && row.progress <= 0,
    quality: asQualityState(row.quality) ?? 'pending',
    document: toDocumentInfo(row.document),
    run: {
      run_id: runId,
      attempt: run?.attempt ?? 1,
      started_at: run?.startedAt ? run.startedAt.toISOString() : null,
      finished_at: run?.finishedAt ? run.finishedAt.toISOString() : null,
    },
    stats,
    validation,
    error: row.errorCode
      ? {
          code: row.errorCode,
          message: row.errorMessage ?? 'Falha na conversão.',
          detail: null,
          retryable: isActive(status) || status === 'failed',
        }
      : null,
    created_at: row.createdAt.toISOString(),
    updated_at: row.updatedAt.toISOString(),
    links: linksFor(row.id),
  };
}

export function toConversionListItem(
  row: ConversionRowShape & { document: DocumentRowShape },
): ConversionListItem {
  const status = asConversionStatus(row.status) ?? 'received';
  const stats = parseJson<RunStats>(row.statsJson);
  return {
    conversion_id: row.id,
    status,
    progress: row.progress,
    quality: asQualityState(row.quality) ?? 'pending',
    filename: row.document.filename,
    page_count: row.document.pageCount,
    size_bytes: row.document.sizeBytes,
    tables: stats ? stats.tables : null,
    cells: stats ? stats.cells : null,
    created_at: row.createdAt.toISOString(),
    updated_at: row.updatedAt.toISOString(),
  };
}

/** Converts the local inspection into the contract's ValidationReport. */
export function toValidationReport(inspection: FileInspection): ValidationReport {
  return {
    ok: inspection.ok,
    is_pdf: inspection.is_pdf,
    pdf_version: inspection.pdf_version,
    page_count: inspection.page_count,
    encrypted: inspection.encrypted,
    needs_password: inspection.needs_password,
    has_forms: inspection.has_forms,
    has_javascript: inspection.has_javascript,
    has_embedded_files: inspection.has_embedded_files,
    has_launch_actions: inspection.has_launch_actions,
    rotation_pages: [],
    warnings: inspection.warnings,
    errors: inspection.errors,
  };
}

export function statusOf(row: ConversionRowShape): ConversionStatus {
  return asConversionStatus(row.status) ?? 'received';
}
