import { createHash } from 'node:crypto';
import type { Readable } from 'node:stream';

import { Inject, Injectable } from '@nestjs/common';

import { ApiError } from '../common/errors/api-error';
import { ERROR_CODES } from '../common/errors/error-codes';

export interface UploadLimits {
  /** @deprecated `null` means no page ceiling (full-document processing). */
  maxBytes: number;
  maxPages: number | null;
}

export const UPLOAD_LIMITS = 'UPLOAD_LIMITS';

export interface UploadFileMeta {
  filename?: string | null;
  mimeType?: string | null;
}

export interface FileInspection {
  ok: boolean;
  is_pdf: boolean;
  kind?: 'pdf' | 'image' | 'spreadsheet' | 'unknown';
  spreadsheet_format?: 'xlsx' | 'csv' | null;
  image_format?: string | null;
  mime_type?: string;
  pdf_version: string | null;
  page_count: number;
  page_count_estimated: boolean;
  encrypted: boolean;
  needs_password: boolean;
  has_forms: boolean;
  has_javascript: boolean;
  has_embedded_files: boolean;
  has_launch_actions: boolean;
  size_bytes: number;
  sha256: string;
  warnings: string[];
  errors: string[];
}

const PDF_HEADER = '%PDF-';
const MAGIC_SCAN_BYTES = 1024;
/**
 * Junk before the PDF header is tolerated up to this offset, the same rule the
 * engine applies (`_looks_like_pdf` in engine/inspector/pdf_inspector.py).
 */
const PDF_HEADER_MAX_OFFSET = 512;
/** Two-byte signatures that also match plain text (a CSV may begin with "BM" or "P1"). */
const WEAK_IMAGE_FORMATS: ReadonlySet<string> = new Set(['BMP', 'PNM']);
// `/Type /Page` but not `/Type /Pages`, tolerant to any whitespace between.
const PAGE_TYPE_PATTERN = /\/Type\s*\/Page(?![A-Za-z])/g;

/** Offset of the `%PDF-` header inside the scan window, or -1 when there is none. */
function findPdfHeader(head: Buffer): number {
  const slice = head.subarray(0, MAGIC_SCAN_BYTES).toString('latin1');
  const index = slice.indexOf(PDF_HEADER);
  return index >= 0 && index <= PDF_HEADER_MAX_OFFSET ? index : -1;
}

export interface HeaderClassification {
  kind: 'pdf' | 'image' | 'spreadsheet' | 'unknown';
  is_pdf: boolean;
  image_format: string | null;
  spreadsheet_format: 'xlsx' | 'csv' | null;
  mime_type: string | null;
  pdf_version: string | null;
}

/**
 * Classifies a document from its first kilobyte only.
 *
 * Order matters: a strong image signature at offset 0 wins over a `%PDF-` text
 * found further in (an image can carry that text in its metadata), and weak
 * two-byte image signatures are only considered after the PDF and spreadsheet
 * checks, so a declared CSV that starts with "BM" stays a CSV.
 */
function classifyHeader(head: Buffer, declaredKind?: string | null): HeaderClassification {
  const spreadsheet = declaredKind === 'spreadsheet';
  const pdfAt = findPdfHeader(head);
  const image = detectImageFormat(head);
  const strongImage = image !== null && !WEAK_IMAGE_FORMATS.has(image) ? image : null;

  if (strongImage && pdfAt !== 0 && !spreadsheet) return imageClassification(strongImage);
  if (pdfAt === 0 || (pdfAt > 0 && !spreadsheet)) return pdfClassification(head, pdfAt);

  if (spreadsheet) {
    if (head.subarray(0, 4).toString('latin1') === 'PK\x03\x04') {
      return {
        kind: 'spreadsheet',
        is_pdf: false,
        image_format: null,
        spreadsheet_format: 'xlsx',
        mime_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        pdf_version: null,
      };
    }
    // CSV candidate: decodable text without pdf/image/zip magic. The engine
    // enforces dialect/encoding (CSV_ENCODING_REQUIRED / CSV_DIALECT_REQUIRED).
    return {
      kind: 'spreadsheet',
      is_pdf: false,
      image_format: null,
      spreadsheet_format: 'csv',
      mime_type: 'text/csv',
      pdf_version: null,
    };
  }
  if (image) return imageClassification(image);
  return {
    kind: 'unknown',
    is_pdf: false,
    image_format: null,
    spreadsheet_format: null,
    mime_type: null,
    pdf_version: null,
  };
}

function imageClassification(format: string): HeaderClassification {
  return {
    kind: 'image',
    is_pdf: false,
    image_format: format,
    spreadsheet_format: null,
    mime_type: mimeForImageFormat(format),
    pdf_version: null,
  };
}

function pdfClassification(head: Buffer, at: number): HeaderClassification {
  const window = head.subarray(at, at + 16).toString('latin1');
  const match = /^%PDF-(\d+\.\d+)/.exec(window);
  return {
    kind: 'pdf',
    is_pdf: true,
    image_format: null,
    spreadsheet_format: null,
    mime_type: 'application/pdf',
    pdf_version: match ? match[1] : null,
  };
}

/**
 * Content-based image detection (signatures), mirroring the engine registry.
 * The extension is never trusted; the engine re-detects the real bytes.
 */
export function detectImageFormat(buffer: Buffer): string | null {
  const head = buffer.subarray(0, 64);
  if (head.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) {
    return 'PNG';
  }
  if (head[0] === 0xff && head[1] === 0xd8 && head[2] === 0xff) return 'JPEG';
  if (head.subarray(0, 6).toString('latin1') === 'GIF87a' || head.subarray(0, 6).toString('latin1') === 'GIF89a') {
    return 'GIF';
  }
  if (head.subarray(0, 2).toString('latin1') === 'BM') return 'BMP';
  if (head.subarray(0, 4).toString('latin1') === 'II*\u0000' || head.subarray(0, 4).toString('latin1') === 'MM\u0000*') {
    return 'TIFF';
  }
  if (head.subarray(0, 4).toString('latin1') === 'RIFF' && head.subarray(8, 12).toString('latin1') === 'WEBP') {
    return 'WEBP';
  }
  if (head[0] === 0x00 && head[1] === 0x00 && head[2] === 0x01 && head[3] === 0x00) return 'ICO';
  if (head.subarray(0, 12).toString('latin1') === '\u0000\u0000\u0000\u000cjP  ') return 'JPEG2000';
  const pnm = head.subarray(0, 2).toString('latin1');
  if (/^P[1-6]$/.test(pnm)) return 'PNM';
  if (head.subarray(4, 8).toString('latin1') === 'ftyp') {
    const brand = head.subarray(8, 12).toString('latin1');
    if (['avif', 'avis'].includes(brand)) return 'AVIF';
    if (['heic', 'heix', 'hevc', 'mif1', 'msf1'].includes(brand)) return 'HEIF';
  }
  return null;
}

export function mimeForImageFormat(format: string): string {
  const map: Record<string, string> = {
    PNG: 'image/png',
    JPEG: 'image/jpeg',
    GIF: 'image/gif',
    BMP: 'image/bmp',
    TIFF: 'image/tiff',
    WEBP: 'image/webp',
    ICO: 'image/x-icon',
    JPEG2000: 'image/jp2',
    PNM: 'image/x-portable-pixmap',
    AVIF: 'image/avif',
    HEIF: 'image/heif',
  };
  return map[format] ?? 'application/octet-stream';
}

export function extensionForImageFormat(format: string): string {
  const map: Record<string, string> = {
    JPEG: 'jpg',
    TIFF: 'tif',
    JPEG2000: 'jp2',
  };
  return map[format] ?? format.toLowerCase();
}

/**
 * Defensive PDF inspection. This is a gatekeeper, not a parser: the Python
 * engine re-validates everything and owns the final page count.
 */
@Injectable()
export class FileValidationService {
  constructor(@Inject(UPLOAD_LIMITS) private readonly limits: UploadLimits) {}

  /** Throws ApiError for empty / non-PDF / oversized / too-many-pages input. */
  validate(buffer: Buffer, meta: UploadFileMeta = {}): FileInspection {
    const inspection = this.inspect(buffer, meta);

    if (inspection.errors.includes('empty')) {
      throw ApiError.badRequest(ERROR_CODES.INVALID_FILE, { reason: 'empty_file' });
    }
    if (inspection.errors.includes('too_large')) {
      throw ApiError.badRequest(ERROR_CODES.FILE_TOO_LARGE, {
        reason: 'file_too_large',
        max_bytes: this.limits.maxBytes,
        size_bytes: inspection.size_bytes,
      });
    }
    if (!inspection.is_pdf) {
      if (!inspection.image_format) {
        throw ApiError.badRequest(ERROR_CODES.INVALID_FILE, {
          reason: 'invalid_magic_bytes',
          filename: meta.filename ?? null,
        });
      }
      // Images produce TXT; frame/pixel limits are enforced by the engine
      // before it decodes (see CONVERTER_MAX_IMAGE_* settings).
      return inspection;
    }
    // There is no page ceiling by default: `maxPages === null` means the
    // document is processed in full. Only an operator who explicitly sets
    // UPLOAD_MAX_PAGES keeps a legacy cap on this endpoint.
    if (this.limits.maxPages !== null && inspection.page_count > this.limits.maxPages) {
      throw ApiError.badRequest(ERROR_CODES.TOO_MANY_PAGES, {
        reason: 'too_many_pages',
        max_pages: this.limits.maxPages,
        page_count: inspection.page_count,
      });
    }

    return inspection;
  }

  /** Never throws; returns the report so callers can persist warnings. */
  inspect(buffer: Buffer, meta: UploadFileMeta = {}): FileInspection {
    const warnings: string[] = [];
    const errors: string[] = [];
    const size = buffer?.length ?? 0;

    if (!buffer || size === 0) {
      return {
        ok: false,
        is_pdf: false,
        pdf_version: null,
        page_count: 0,
        page_count_estimated: false,
        encrypted: false,
        needs_password: false,
        has_forms: false,
        has_javascript: false,
        has_embedded_files: false,
        has_launch_actions: false,
        size_bytes: size,
        sha256: '',
        warnings,
        errors: ['empty'],
      };
    }

    if (size > this.limits.maxBytes) {
      // Still inspect the header so the caller gets a faithful report.
      const classified = classifyHeader(buffer.subarray(0, MAGIC_SCAN_BYTES));
      return {
        ok: false,
        is_pdf: classified.is_pdf,
        pdf_version: classified.pdf_version,
        page_count: 0,
        page_count_estimated: false,
        encrypted: false,
        needs_password: false,
        has_forms: false,
        has_javascript: false,
        has_embedded_files: false,
        has_launch_actions: false,
        size_bytes: size,
        sha256: this.sha256(buffer),
        warnings,
        errors: ['too_large'],
      };
    }

    const classified = classifyHeader(buffer.subarray(0, MAGIC_SCAN_BYTES));
    const isPdf = classified.is_pdf;
    const imageFormat = classified.image_format;
    if (!isPdf && !imageFormat) errors.push('invalid_header');

    const body = buffer.toString('latin1');
    const pageCount = isPdf ? this.countPages(body) : 0;
    if (isPdf && pageCount === 0) {
      warnings.push('page_count_unavailable');
    }

    const encrypted = isPdf && /\/Encrypt\b/.test(body);
    if (encrypted) warnings.push('encrypted_pdf');

    return {
      ok: (isPdf || Boolean(imageFormat)) && errors.length === 0,
      is_pdf: isPdf,
      kind: isPdf ? 'pdf' : imageFormat ? 'image' : 'unknown',
      image_format: imageFormat,
      mime_type: isPdf
        ? 'application/pdf'
        : imageFormat
          ? mimeForImageFormat(imageFormat)
          : 'application/octet-stream',
      pdf_version: classified.pdf_version,
      page_count: pageCount,
      page_count_estimated: pageCount > 0,
      encrypted,
      needs_password: encrypted,
      has_forms: isPdf && /\/AcroForm\b/.test(body),
      has_javascript: isPdf && /\/JavaScript\b|\/JS\b/.test(body),
      has_embedded_files: isPdf && /\/EmbeddedFiles\b/.test(body),
      has_launch_actions: isPdf && /\/Launch\b/.test(body),
      size_bytes: size,
      sha256: this.sha256(buffer),
      warnings,
      errors,
    };
  }

  sha256(buffer: Buffer): string {
    return createHash('sha256').update(buffer).digest('hex');
  }

  /**
   * Header-only inspection for the resumable path.
   *
   * The caller has already streamed the document to storage, so nothing here
   * buffers it: only the first kilobyte decides PDF vs image. The engine
   * re-validates the whole file and owns the authoritative page count.
   */
  /**
   * Header-only detection. `declaredKind` admits spreadsheet items: XLSX is
   * ZIP magic here (OOXML markers live in the central directory, checked by
   * the engine) and CSV is declared text — the engine validates both fully.
   */
  inspectHeader(head: Buffer, declaredKind?: string | null): HeaderClassification {
    return classifyHeader(head, declaredKind);
  }

  /**
   * Counts `/Type /Page` across a stream with a small overlap window.
   *
   * Object streams are not expanded here, so the count can be an
   * underestimate; that is reported as `page_count_estimated` and the engine
   * replaces it. This is a progress hint, never an admission gate.
   */
  async countPagesStreaming(stream: Readable): Promise<number> {
    const pattern = new RegExp(PAGE_TYPE_PATTERN.source, 'g');
    let total = 0;
    let carry = '';
    for await (const chunk of stream) {
      const text = carry + Buffer.from(chunk as Uint8Array).toString('latin1');
      pattern.lastIndex = 0;
      let match: RegExpExecArray | null;
      while ((match = pattern.exec(text)) !== null) total += 1;
      carry = text.slice(-32);
    }
    return total;
  }

  /**
   * Counts `/Type /Page` occurrences. PDFs using object streams return 0; the
   * engine then reports the authoritative count. `/Type /Pages` never matches.
   */
  countPages(body: string): number {
    const matches = body.match(PAGE_TYPE_PATTERN);
    return matches ? matches.length : 0;
  }
}
