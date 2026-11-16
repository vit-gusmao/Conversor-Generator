import { createHash } from 'node:crypto';

import { Inject, Injectable } from '@nestjs/common';

import { ApiError } from '../common/errors/api-error';
import { ERROR_CODES } from '../common/errors/error-codes';

export interface UploadLimits {
  maxBytes: number;
  maxPages: number;
}

export const UPLOAD_LIMITS = 'UPLOAD_LIMITS';

export interface UploadFileMeta {
  filename?: string | null;
  mimeType?: string | null;
}

export interface FileInspection {
  ok: boolean;
  is_pdf: boolean;
  kind?: 'pdf' | 'image' | 'unknown';
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
// `/Type /Page` but not `/Type /Pages`, tolerant to any whitespace between.
const PAGE_TYPE_PATTERN = /\/Type\s*\/Page(?![A-Za-z])/g;

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
    if (inspection.page_count > this.limits.maxPages) {
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
      const headerSlice = buffer.subarray(0, MAGIC_SCAN_BYTES).toString('latin1');
      return {
        ok: false,
        is_pdf: headerSlice.includes(PDF_HEADER),
        pdf_version: this.readVersion(buffer),
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

    const head = buffer.subarray(0, MAGIC_SCAN_BYTES).toString('latin1');
    const isPdf = head.includes(PDF_HEADER);
    const imageFormat = isPdf ? null : detectImageFormat(buffer);
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
      pdf_version: isPdf ? this.readVersion(buffer) : null,
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
   * Counts `/Type /Page` occurrences. PDFs using object streams return 0; the
   * engine then reports the authoritative count. `/Type /Pages` never matches.
   */
  countPages(body: string): number {
    const matches = body.match(PAGE_TYPE_PATTERN);
    return matches ? matches.length : 0;
  }

  private readVersion(buffer: Buffer): string | null {
    const head = buffer.subarray(0, MAGIC_SCAN_BYTES).toString('latin1');
    const index = head.indexOf(PDF_HEADER);
    if (index < 0) return null;
    const match = /^%PDF-(\d\.\d)/.exec(head.slice(index, index + 16));
    return match ? match[1] : null;
  }
}
