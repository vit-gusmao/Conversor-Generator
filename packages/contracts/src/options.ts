/**
 * Canonical conversion options (`ConversionOptionsV2`).
 * ============================================================================
 * One normalizer shared by the browser, the NestJS gateway and (mirrored in
 * Python) the converter engine. The wire format is snake_case.
 *
 * Design rules enforced here:
 *   - No page ceiling. `max_pages` / `maxPages` anywhere in the input is an
 *     `INVALID_OPTION`, never a silently applied cap.
 *   - Images can only produce TXT; asking for XLSX/CSV explicitly is an error,
 *     omitting `target_formats` picks the sensible default for the kind.
 *   - Legacy flat keys are still accepted so the pre-existing single-file
 *     `POST /conversions` keeps working for old clients.
 */

import { z } from 'zod';

export const OUTPUT_FORMATS = ['xlsx', 'txt', 'csv'] as const;
export type OutputFormat = (typeof OUTPUT_FORMATS)[number];

export const OCR_STRATEGIES = ['auto', 'native', 'ocr', 'hybrid'] as const;
export type OcrStrategy = (typeof OCR_STRATEGIES)[number];

export const NEWLINE_MODES = ['lf', 'crlf'] as const;
export type NewlineMode = (typeof NEWLINE_MODES)[number];

export const PAGE_SEPARATORS = ['form_feed', 'marker', 'none'] as const;
export type PageSeparator = (typeof PAGE_SEPARATORS)[number];

export const CSV_PROFILES = ['interoperable', 'excel_br'] as const;
export type CsvProfile = (typeof CSV_PROFILES)[number];

export const FORMULA_POLICIES = ['safe', 'raw'] as const;
export type FormulaPolicy = (typeof FORMULA_POLICIES)[number];

export const VALUE_MODES = ['original', 'canonical'] as const;
export type ValueMode = (typeof VALUE_MODES)[number];

export interface OcrOptionsV2 {
  enabled: boolean;
  languages: string[];
  dpi: number | null;
  strategy: OcrStrategy;
}

export interface TextOptionsV2 {
  newline: NewlineMode;
  page_separator: PageSeparator;
  include_table_text: boolean;
}

export interface CsvOptionsV2 {
  profile: CsvProfile;
  delimiter: string | null;
  formula_policy: FormulaPolicy;
  table_ids: string[] | null;
  value_mode: ValueMode;
}

export interface ConversionOptionsV2 {
  schema_version: 2;
  target_formats: OutputFormat[];
  ocr: OcrOptionsV2;
  text: TextOptionsV2;
  csv: CsvOptionsV2;
}

/** Error codes surfaced to the HTTP envelope. */
export type OptionsErrorCode =
  | 'INVALID_OPTION'
  | 'UNSUPPORTED_TARGET_FORMAT'
  | 'INCOMPATIBLE_OUTPUT_FORMAT';

export class ConversionOptionsError extends Error {
  readonly code: OptionsErrorCode;
  readonly details: Record<string, unknown>;

  constructor(code: OptionsErrorCode, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.name = 'ConversionOptionsError';
    this.code = code;
    this.details = details;
  }
}

const DEFAULT_OCR: OcrOptionsV2 = {
  enabled: true,
  languages: ['por', 'eng'],
  dpi: null,
  strategy: 'auto',
};

const DEFAULT_TEXT: TextOptionsV2 = {
  newline: 'lf',
  page_separator: 'form_feed',
  include_table_text: true,
};

const DEFAULT_CSV: CsvOptionsV2 = {
  profile: 'interoperable',
  delimiter: null,
  formula_policy: 'safe',
  table_ids: null,
  value_mode: 'original',
};

/**
 * Defaults when nothing is sent. `kind === 'image'` yields TXT only, because
 * the conversion matrix defines image → TXT and the engine has no spreadsheet
 * writer for raster input.
 */
export function defaultConversionOptions(
  kind: 'pdf' | 'image' | 'unknown' = 'pdf',
): ConversionOptionsV2 {
  return {
    schema_version: 2,
    target_formats: kind === 'image' ? ['txt'] : ['xlsx', 'txt', 'csv'],
    ocr: { ...DEFAULT_OCR, languages: [...DEFAULT_OCR.languages] },
    text: { ...DEFAULT_TEXT },
    csv: { ...DEFAULT_CSV },
  };
}

const ocrSchema = z
  .object({
    enabled: z.boolean().optional(),
    languages: z.array(z.string().min(1).max(16)).max(8).optional(),
    dpi: z.number().int().min(72).max(600).nullable().optional(),
    strategy: z.enum(OCR_STRATEGIES).optional(),
  })
  .strict();

const textSchema = z
  .object({
    newline: z.enum(NEWLINE_MODES).optional(),
    page_separator: z.enum(PAGE_SEPARATORS).optional(),
    include_table_text: z.boolean().optional(),
  })
  .strict();

const csvSchema = z
  .object({
    profile: z.enum(CSV_PROFILES).optional(),
    delimiter: z
      .string()
      .min(1)
      .max(1)
      .nullable()
      .optional(),
    formula_policy: z.enum(FORMULA_POLICIES).optional(),
    table_ids: z.array(z.string().min(1)).nullable().optional(),
    value_mode: z.enum(VALUE_MODES).optional(),
  })
  .strict();

const optionsSchema = z
  .object({
    schema_version: z.literal(2).optional(),
    target_formats: z.array(z.enum(OUTPUT_FORMATS)).min(1).max(OUTPUT_FORMATS.length).optional(),
    ocr: ocrSchema.optional(),
    text: textSchema.optional(),
    csv: csvSchema.optional(),
  })
  .strict();

/** Keys that used to cap the document. Their presence is always an error. */
const CEILING_KEYS = ['max_pages', 'maxPages', 'max_pages_per_document', 'page_limit'] as const;

/** Legacy flat aliases accepted from older clients of `POST /conversions`. */
const LEGACY_KEYS = new Set([
  'target_formats',
  'ocr',
  'ocr_enabled',
  'dpi',
  'text_newline',
  'page_separator',
  'csv_profile',
  'csv_delimiter',
  'csv_table_ids',
  'csv_formula_policy',
  'csv_value_mode',
  'include_table_text',
]);

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function rejectCeilings(input: Record<string, unknown>): void {
  for (const key of CEILING_KEYS) {
    if (key in input && input[key] !== null && input[key] !== undefined) {
      throw new ConversionOptionsError(
        'INVALID_OPTION',
        'O limite de páginas por requisição não é suportado: o fluxo padrão processa o documento inteiro.',
        { option: key },
      );
    }
  }
}

/** Rewrites legacy flat keys into the nested v2 shape. */
function fromLegacy(input: Record<string, unknown>): Record<string, unknown> {
  const nested: Record<string, unknown> = {};
  if ('target_formats' in input) nested.target_formats = input.target_formats;

  const ocr: Record<string, unknown> = {};
  if (typeof input.ocr === 'boolean') ocr.enabled = input.ocr;
  if (typeof input.ocr_enabled === 'boolean') ocr.enabled = input.ocr_enabled;
  if (isPlainObject(input.ocr)) Object.assign(ocr, fromNestedOcr(input.ocr));
  if (input.dpi !== undefined && input.dpi !== null) ocr.dpi = input.dpi;
  if (Object.keys(ocr).length > 0) nested.ocr = ocr;

  const text: Record<string, unknown> = {};
  if (input.text_newline !== undefined) text.newline = input.text_newline;
  if (input.page_separator !== undefined) text.page_separator = input.page_separator;
  if (typeof input.include_table_text === 'boolean') text.include_table_text = input.include_table_text;
  if (isPlainObject(input.text)) Object.assign(text, input.text);
  if (Object.keys(text).length > 0) nested.text = text;

  const csv: Record<string, unknown> = {};
  if (input.csv_profile !== undefined) csv.profile = input.csv_profile;
  if (input.csv_delimiter !== undefined) csv.delimiter = input.csv_delimiter;
  if (input.csv_table_ids !== undefined) csv.table_ids = input.csv_table_ids;
  if (input.csv_formula_policy !== undefined) csv.formula_policy = input.csv_formula_policy;
  if (input.csv_value_mode !== undefined) csv.value_mode = input.csv_value_mode;
  if (isPlainObject(input.csv)) Object.assign(csv, input.csv);
  if (Object.keys(csv).length > 0) nested.csv = csv;

  return nested;
}

function fromNestedOcr(value: Record<string, unknown>): Record<string, unknown> {
  const allowed = ['enabled', 'languages', 'dpi', 'strategy'];
  const result: Record<string, unknown> = {};
  for (const key of allowed) {
    if (value[key] !== undefined) result[key] = value[key];
  }
  return result;
}

export interface NormalizeOptionsInput {
  /** Content kind detected by the caller. Drives the default target formats. */
  kind?: 'pdf' | 'image' | 'unknown';
  /** When false, the presence of unknown keys is an error (default: true). */
  allowLegacy?: boolean;
}

/**
 * Normalizes any accepted options payload into `ConversionOptionsV2`.
 *
 * @throws ConversionOptionsError with `INVALID_OPTION` or
 *         `INCOMPATIBLE_OUTPUT_FORMAT` on invalid input.
 */
export function normalizeConversionOptions(
  input: unknown,
  options: NormalizeOptionsInput = {},
): ConversionOptionsV2 {
  const kind = options.kind ?? 'pdf';
  const allowLegacy = options.allowLegacy ?? true;
  const base = defaultConversionOptions(kind);

  if (input === undefined || input === null) return base;

  if (!isPlainObject(input)) {
    throw new ConversionOptionsError('INVALID_OPTION', 'As opções devem ser um objeto.', {
      received: typeof input,
    });
  }

  rejectCeilings(input);

  const unknownKeys = Object.keys(input).filter(
    (key) =>
      !LEGACY_KEYS.has(key) &&
      !(CEILING_KEYS as readonly string[]).includes(key) &&
      !['schema_version', 'ocr', 'text', 'csv'].includes(key),
  );
  if (unknownKeys.length > 0) {
    throw new ConversionOptionsError('INVALID_OPTION', 'Opção desconhecida.', {
      keys: unknownKeys,
    });
  }

  const normalised = allowLegacy ? fromLegacy(input) : (input as Record<string, unknown>);
  const parsed = optionsSchema.safeParse(normalised);

  if (!parsed.success) {
    throw new ConversionOptionsError('INVALID_OPTION', 'Opções inválidas.', {
      issues: parsed.error.issues.map((issue) => ({
        path: issue.path.join('.'),
        message: issue.message,
      })),
    });
  }

  const data = parsed.data;
  const explicitFormats = data.target_formats !== undefined;
  const targetFormats = explicitFormats
    ? Array.from(new Set(data.target_formats as OutputFormat[]))
    : [...base.target_formats];

  if (kind === 'image') {
    const incompatible = targetFormats.filter((format) => format !== 'txt');
    if (incompatible.length > 0) {
      throw new ConversionOptionsError(
        'INCOMPATIBLE_OUTPUT_FORMAT',
        'Imagens são reconhecidas por OCR e só podem ser entregues como TXT.',
        { formats: incompatible, allowed: ['txt'] },
      );
    }
  }

  return {
    schema_version: 2,
    target_formats: kind === 'image' ? ['txt'] : targetFormats,
    ocr: {
      enabled: data.ocr?.enabled ?? base.ocr.enabled,
      languages: data.ocr?.languages ? [...data.ocr.languages] : base.ocr.languages,
      dpi: data.ocr?.dpi !== undefined ? data.ocr.dpi : base.ocr.dpi,
      strategy: data.ocr?.strategy ?? base.ocr.strategy,
    },
    text: {
      newline: data.text?.newline ?? base.text.newline,
      page_separator: data.text?.page_separator ?? base.text.page_separator,
      include_table_text: data.text?.include_table_text ?? base.text.include_table_text,
    },
    csv: {
      profile: data.csv?.profile ?? base.csv.profile,
      delimiter: data.csv?.delimiter !== undefined ? data.csv.delimiter : base.csv.delimiter,
      formula_policy: data.csv?.formula_policy ?? base.csv.formula_policy,
      table_ids: data.csv?.table_ids !== undefined ? data.csv.table_ids : base.csv.table_ids,
      value_mode: data.csv?.value_mode ?? base.csv.value_mode,
    },
  };
}

/**
 * Deterministic JSON for hashing (idempotency fingerprints).
 * Keys are emitted in a fixed order so equal options always produce equal text.
 */
export function canonicalOptionsJson(options: ConversionOptionsV2): string {
  return JSON.stringify({
    schema_version: options.schema_version,
    target_formats: [...options.target_formats].sort(),
    ocr: {
      enabled: options.ocr.enabled,
      languages: [...options.ocr.languages],
      dpi: options.ocr.dpi,
      strategy: options.ocr.strategy,
    },
    text: {
      newline: options.text.newline,
      page_separator: options.text.page_separator,
      include_table_text: options.text.include_table_text,
    },
    csv: {
      profile: options.csv.profile,
      delimiter: options.csv.delimiter,
      formula_policy: options.csv.formula_policy,
      table_ids: options.csv.table_ids ? [...options.csv.table_ids] : null,
      value_mode: options.csv.value_mode,
    },
  });
}

/** True when the options can produce a spreadsheet artifact. */
export function producesSpreadsheet(options: ConversionOptionsV2): boolean {
  return options.target_formats.includes('xlsx');
}
