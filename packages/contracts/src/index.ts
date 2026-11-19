/**
 * @pdf2x/contracts
 * ============================================================================
 * Single source of truth for the public contract between:
 *   - apps/web  (React + Vite frontend)
 *   - apps/mcp  (MCP adapter)
 *   - apps/api  (NestJS public REST API)
 *   - services/converter (FastAPI internal API, mirrors these shapes)
 *
 * Any change here must be reflected in
 * services/converter/models/api_contract.py and api/schemas.py
 * ============================================================================
 */

export * from './options';
export * from './batches';
export * from './uploads';
export * from './contract-errors';
export * from './sources';
export * from './schedules';
export * from './runtime';

/* ------------------------------------------------------------------------- */
/* Status and pipeline                                                        */
/* ------------------------------------------------------------------------- */

export const CONVERSION_STATUSES = [
  'received',
  'validating',
  'queued',
  'inspecting',
  'extracting',
  'structuring',
  'normalizing',
  'reconciling',
  'generating',
  'auditing',
  'completed',
  'completed_with_warnings',
  'failed',
  'cancelled',
] as const;

export type ConversionStatus = (typeof CONVERSION_STATUSES)[number];

export const TERMINAL_STATUSES: readonly ConversionStatus[] = [
  'completed',
  'completed_with_warnings',
  'failed',
  'cancelled',
];

export const ACTIVE_STATUSES: readonly ConversionStatus[] = CONVERSION_STATUSES.filter(
  (s) => !TERMINAL_STATUSES.includes(s),
);

export function isTerminal(status: ConversionStatus): boolean {
  return TERMINAL_STATUSES.includes(status);
}

export function isActive(status: ConversionStatus): boolean {
  return !isTerminal(status);
}

/** The eight stages the UI renders. Weights sum to 1. */
export const PIPELINE_STAGES = [
  { key: 'receive', label: 'Recebendo e validando o PDF', weight: 0.05, statuses: ['received', 'validating'] },
  { key: 'inspect', label: 'Inspecionando páginas e regiões', weight: 0.1, statuses: ['queued', 'inspecting'] },
  { key: 'extract', label: 'Extraindo texto nativo e OCR', weight: 0.25, statuses: ['extracting'] },
  { key: 'structure', label: 'Detectando campos, tabelas e registros', weight: 0.2, statuses: ['structuring'] },
  { key: 'normalize', label: 'Normalizando e reconstruindo dados', weight: 0.1, statuses: ['normalizing'] },
  { key: 'reconcile', label: 'Conciliando páginas e registros', weight: 0.05, statuses: ['reconciling'] },
  { key: 'generate', label: 'Gerando o Excel', weight: 0.15, statuses: ['generating'] },
  { key: 'audit', label: 'Executando auditoria final', weight: 0.1, statuses: ['auditing'] },
] as const;

export type StageKey = (typeof PIPELINE_STAGES)[number]['key'];

export function stageIndexForStatus(status: ConversionStatus): number {
  if (status === 'completed' || status === 'completed_with_warnings') return PIPELINE_STAGES.length;
  if (status === 'failed' || status === 'cancelled') return -1;
  const idx = PIPELINE_STAGES.findIndex((s) => (s.statuses as readonly string[]).includes(status));
  return idx < 0 ? 0 : idx;
}

/* ------------------------------------------------------------------------- */
/* Quality                                                                    */
/* ------------------------------------------------------------------------- */

export const QUALITY_STATES = ['validated', 'partial', 'failed', 'pending'] as const;
export type QualityState = (typeof QUALITY_STATES)[number];

export const SEVERITIES = ['info', 'warning', 'error', 'critical'] as const;
export type Severity = (typeof SEVERITIES)[number];

export const VALUE_TYPES = [
  'text',
  'integer',
  'decimal',
  'money',
  'date',
  'datetime',
  'percentage',
  'identifier',
  'code',
  'competencia',
  'boolean',
  'empty',
  'unknown',
] as const;
export type ValueType = (typeof VALUE_TYPES)[number];

/* ------------------------------------------------------------------------- */
/* Core resources                                                             */
/* ------------------------------------------------------------------------- */

export interface User {
  id: string;
  email: string | null;
  name: string | null;
  role: 'owner' | 'admin' | 'viewer';
  is_anonymous: boolean;
  created_at: string;
}

export interface AuthSession {
  access_token: string;
  token_type: 'Bearer';
  expires_in: number;
  user: User;
}

export interface DocumentInfo {
  id: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  sha256: string;
  page_count: number | null;
  created_at: string;
}

export interface ValidationReport {
  ok: boolean;
  is_pdf: boolean;
  pdf_version: string | null;
  page_count: number;
  encrypted: boolean;
  needs_password: boolean;
  has_forms: boolean;
  has_javascript: boolean;
  has_embedded_files: boolean;
  has_launch_actions: boolean;
  rotation_pages: number[];
  warnings: string[];
  errors: string[];
}

export interface RunStats {
  pages_total: number;
  pages_processed: number;
  pages_failed: number;
  tables: number;
  fields: number;
  records: number;
  sheets: number;
  cells: number;
  text_blocks: number;
  blocks_assigned: number;
  blocks_preserved: number;
  blocks_unclassified: number;
  ocr_regions: number;
  ocr_pages: number;
  warnings: number;
  errors: number;
  duration_ms: number;
  coverage_percent: number;
}

export interface ConversionError {
  code: string;
  message: string;
  detail?: string | null;
  retryable: boolean;
}

export interface Conversion {
  conversion_id: string;
  status: ConversionStatus;
  stage: StageKey | 'done' | 'failed';
  progress: number;
  progress_indeterminate: boolean;
  quality: QualityState;
  document: DocumentInfo;
  run: {
    run_id: string;
    attempt: number;
    started_at: string | null;
    finished_at: string | null;
  };
  stats: RunStats | null;
  validation: ValidationReport | null;
  error: ConversionError | null;
  created_at: string;
  updated_at: string;
  links: Record<string, string>;
}

export interface ConversionListItem {
  conversion_id: string;
  status: ConversionStatus;
  progress: number;
  quality: QualityState;
  filename: string;
  page_count: number | null;
  size_bytes: number;
  tables: number | null;
  cells: number | null;
  created_at: string;
  updated_at: string;
}

export interface Paginated<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

/* ------------------------------------------------------------------------- */
/* Result / preview                                                           */
/* ------------------------------------------------------------------------- */

export interface SheetSummary {
  name: string;
  kind: 'records' | 'table' | 'text' | 'unclassified' | 'metadata' | 'consolidated';
  rows: number;
  columns: number;
  source_pages: number[];
  table_id: string | null;
}

export interface ResultSummary {
  conversion_id: string;
  status: ConversionStatus;
  quality: QualityState;
  pages: number;
  tables: number;
  sheets_count: number;
  cells: number;
  records: number;
  fields: number;
  download_url: string;
  audit_url: string;
  sheets: SheetSummary[];
  warnings: AuditIssue[];
}

/* ------------------------------------------------------------------------- */
/* Compact result overview (U1)                                               */
/* ------------------------------------------------------------------------- */

/**
 * One row of the sheet selector. Deliberately small: `source_pages` is not
 * inlined here, because a 10,000-page document would otherwise put ten
 * thousand numbers in the overview payload. Use the paginated sheet endpoint
 * for the full page list.
 */
export interface SheetDescriptor {
  id: string;
  name: string;
  kind: SheetSummary['kind'];
  rows: number;
  columns: number;
  source_page_count: number;
  table_id: string | null;
}

export const PUBLICATION_STATUSES = ['approved', 'quarantined', 'expired', 'pending'] as const;
export type PublicationStatus = (typeof PUBLICATION_STATUSES)[number];

/**
 * Payload size of this object is independent of the total cell count, so a
 * million-cell workbook returns the same shape as a tiny one.
 */
export interface ResultOverview {
  conversion_id: string;
  run_id: string;
  status: ConversionStatus;
  quality: QualityState;
  publication_status: PublicationStatus;
  counts: {
    pages: number;
    tables: number;
    sheets: number;
    cells: number;
    records: number;
    fields: number;
  };
  available_formats: string[];
  /** Sheet the UI should open first; `null` when the workbook has no sheets. */
  default_sheet: string | null;
  warning_counts: {
    total: number;
    error: number;
    critical: number;
    warning: number;
  };
  /** Present when processing finished with failed pages. */
  partial: boolean;
  pages_failed: number;
  pages_total: number;
  download_url: string | null;
  audit_url: string;
}

export interface SheetPage {
  items: SheetDescriptor[];
  next_cursor: string | null;
  total: number;
}

/** Per-page provenance of one sheet, paginated. */
export interface SheetSourcePages {
  sheet_id: string;
  pages: number[];
  next_cursor: string | null;
  total: number;
}

export interface PreviewCell {
  value: string | number | null;
  type: ValueType;
  display: string;
}

export interface Preview {
  sheet: string;
  headers: string[];
  rows: PreviewCell[][];
  total_rows: number;
  offset: number;
  limit: number;
}

/* ------------------------------------------------------------------------- */
/* Audit                                                                      */
/* ------------------------------------------------------------------------- */

export interface AuditEvent {
  event_id: string;
  run_id: string;
  sequence: number;
  event_type: string;
  stage: string;
  severity: Severity;
  page_number: number | null;
  region_id: string | null;
  extraction_method: string | null;
  message: string;
  payload: Record<string, unknown>;
  previous_hash: string | null;
  event_hash: string;
  created_at: string;
}

export interface AuditIssue {
  issue_id: string;
  run_id: string;
  severity: Severity;
  code: string;
  message: string;
  page_number: number | null;
  region_id: string | null;
  context: Record<string, unknown>;
  created_at: string;
}

export interface CoverageRow {
  page_number: number;
  text_blocks: number;
  tokens: number;
  assigned: number;
  preserved: number;
  header_footer: number;
  unclassified: number;
  dropped: number;
  coverage_percent: number;
}

export interface AuditSummary {
  conversion_id: string;
  run_id: string;
  quality: QualityState;
  status: ConversionStatus;
  metrics: {
    pages_analyzed: number;
    pages_total: number;
    blocks_total: number;
    blocks_assigned: number;
    blocks_preserved: number;
    blocks_unclassified: number;
    blocks_dropped: number;
    cells_generated: number;
    tables_detected: number;
    fields_detected: number;
    records_detected: number;
    ocr_regions: number;
    events_total: number;
    issues_total: number;
    warnings: number;
    errors: number;
    coverage_percent: number;
    assignment_percent: number;
  };
  coverage: CoverageRow[];
  engines: EngineUsage[];
  decisions: DecisionSummary[];
  validations: ValidationCheck[];
  timings: StageTiming[];
  hash_chain_valid: boolean;
  generated_at: string;
}

export interface EngineUsage {
  engine: string;
  kind: 'native' | 'table' | 'ocr' | 'layout' | 'vision' | 'semantic';
  regions: number;
  pages: number;
  duration_ms: number;
  accepted: number;
  rejected: number;
}

export interface DecisionSummary {
  decision_id: string;
  stage: string;
  subject: string;
  accepted: boolean;
  algorithm: string;
  reason_codes: string[];
  alternatives: number;
  page_number: number | null;
  at: string;
}

export interface ValidationCheck {
  check: string;
  status: 'pass' | 'fail' | 'warn' | 'skip';
  details: string;
  measured: string | null;
  expected: string | null;
}

export interface StageTiming {
  stage: string;
  started_at: string;
  finished_at: string | null;
  duration_ms: number;
  pages: number;
}

export interface PageAudit {
  page_number: number;
  width: number;
  height: number;
  rotation: number;
  classification: 'native' | 'scan' | 'hybrid' | 'image' | 'mixed' | 'empty';
  status: 'ok' | 'partial' | 'failed' | 'skipped';
  text_source: 'native' | 'ocr' | 'mixed' | 'none';
  native_chars: number;
  ocr_chars: number;
  text_blocks: number;
  tables: number;
  fields: number;
  regions: RegionAudit[];
  blocks: BlockAudit[];
  tables_detail: TableAudit[];
  issues: AuditIssue[];
  metrics: Record<string, number>;
}

export interface RegionAudit {
  region_id: string;
  kind: 'header' | 'footer' | 'title' | 'form' | 'table' | 'paragraph' | 'list' | 'image' | 'unknown';
  bbox: [number, number, number, number];
  confidence: number;
  engine: string;
  resolution: string;
}

export interface BlockAudit {
  element_id: string;
  text: string;
  bbox: [number, number, number, number];
  origin: 'native' | 'ocr' | 'form_field';
  page_number: number;
  font: string | null;
  size: number | null;
  direction: string | null;
  confidence: number | null;
  destination: {
    kind: 'field' | 'table' | 'text' | 'metadata' | 'header_footer' | 'unclassified' | 'dropped';
    sheet: string | null;
    cell: string | null;
    record_id: string | null;
  } | null;
}

export interface TableAudit {
  table_id: string;
  page_number: number;
  bbox: [number, number, number, number];
  strategy: 'lattice' | 'stream' | 'network' | 'hybrid' | 'docling' | 'transposed' | 'manual';
  headers: string[];
  rows_count: number;
  columns_count: number;
  confidence: number;
  merged_cells: Array<{ row: number; col: number; rowspan: number; colspan: number }>;
  continued_from_table_id: string | null;
  continued_on_page: number | null;
  sheet: string | null;
  cell_range: string | null;
  alternatives: Array<{ strategy: string; confidence: number; accepted: boolean; reason: string }>;
}

export interface CellEvidence {
  sheet: string;
  cell: string;
  row: number;
  column: number;
  header: string;
  value: string | null;
  original_value: string | null;
  value_type: ValueType;
  number_format: string | null;
  source: {
    element_id: string;
    page_number: number;
    bbox: [number, number, number, number];
    origin: 'native' | 'ocr' | 'form_field';
    engine: string;
    confidence: number | null;
  } | null;
  transformations: Array<{ rule: string; from: string; to: string; reason: string }>;
  record_id: string | null;
  field_label: string | null;
  table_id: string | null;
  validation: { source_preserved: boolean; cell_written: boolean; value_matches_source: boolean };
}

/* ------------------------------------------------------------------------- */
/* SSE                                                                        */
/* ------------------------------------------------------------------------- */

export interface ConversionEvent {
  sequence: number;
  event_id: string;
  conversion_id: string;
  run_id: string;
  type:
    | 'status'
    | 'stage'
    | 'progress'
    | 'log'
    | 'issue'
    | 'decision'
    | 'metrics'
    | 'completed'
    | 'failed'
    | 'heartbeat';
  status: ConversionStatus;
  stage: StageKey | 'done' | 'failed';
  progress: number;
  progress_indeterminate: boolean;
  message: string;
  payload: Record<string, unknown>;
  at: string;
}

/* ------------------------------------------------------------------------- */
/* Settings                                                                   */
/* ------------------------------------------------------------------------- */

/**
 * Upload capabilities. The UI must use these instead of assuming a 50 MiB
 * fallback: `max_file_bytes` is `null` when the resumable path has no product
 * ceiling, while `max_upload_bytes` remains the explicit limit of the legacy
 * single-shot `POST /conversions`.
 */
export interface UploadCapabilities {
  resumable: boolean;
  accepts_multiple: boolean;
  part_size_bytes: number;
  max_part_bytes: number;
  max_file_bytes: number | null;
  max_upload_bytes: number | null;
  max_files_per_batch: number | null;
}

export interface PlatformSettings {
  auth_mode: 'optional' | 'required';
  max_upload_bytes: number;
  /**
   * @deprecated No page ceiling exists in the default flow. Always `null` on
   * servers that support full-document processing; kept for older clients.
   */
  max_pages: number | null;
  runtime_mode: 'standalone' | 'full';
  ocr_enabled: boolean;
  ocr_engine: string | null;
  storage_backend: 'local' | 's3';
  retention: { pdf_days: number; xlsx_days: number; audit_days: number };
  engine_version: string;
  capabilities: Record<string, boolean>;
  upload?: UploadCapabilities;
}

