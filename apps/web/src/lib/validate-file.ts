import { formatBytes } from '@/lib/format';

export const DEFAULT_MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

export type FileValidationCode = 'EXTENSION' | 'MIME' | 'SIZE' | 'EMPTY' | 'UNSUPPORTED';

export interface FileValidationResult {
  ok: boolean;
  code: FileValidationCode | null;
  message: string | null;
  kind?: 'pdf' | 'image' | 'spreadsheet' | null;
  format?: string | null;
}

export const OK: FileValidationResult = { ok: true, code: null, message: null };

export interface FileValidationOptions {
  maxBytes?: number | null;
}

/**
 * Formats declared by the server capability registry.  The list is used for
 * immediate feedback only; the API re-detects the real content (a `.png`
 * extension on a PDF is still a PDF).
 */
export const IMAGE_EXTENSIONS = [
  'png',
  'jpg',
  'jpeg',
  'bmp',
  'webp',
  'tif',
  'tiff',
  'gif',
  'ico',
  'jp2',
  'pnm',
  'avif',
  'heic',
  'heif',
] as const;

export const SPREADSHEET_EXTENSIONS = ['xlsx', 'csv'] as const;

export const ACCEPTED_DOCUMENT_ACCEPT = [
  'application/pdf',
  '.pdf',
  ...IMAGE_EXTENSIONS.map((extension) => `.${extension}`),
  'image/*',
  '.xlsx',
  '.csv',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  'text/csv',
].join(',');

function extensionOf(name: string): string {
  const match = /\.([a-z0-9]+)$/i.exec(name.trim());
  return match ? match[1].toLowerCase() : '';
}

function imageFormatFor(extension: string): string | null {
  if (extension === 'jpg' || extension === 'jpeg') return 'JPEG';
  if (!(IMAGE_EXTENSIONS as readonly string[]).includes(extension)) return null;
  if (extension === 'tif') return 'TIFF';
  if (extension === 'jp2') return 'JPEG2000';
  return extension.toUpperCase();
}

/**
 * Client-side gate before upload. The API remains the source of truth, but
 * this avoids wasting a request and gives immediate, accessible feedback.
 */
export function validateDocumentFile(
  file: Pick<File, 'name' | 'size' | 'type'>,
  options: FileValidationOptions = {},
): FileValidationResult {
  // `undefined` keeps the legacy default; `null` means the resumable upload
  // path has no client-side size ceiling.
  const maxBytes =
    options.maxBytes === undefined ? DEFAULT_MAX_UPLOAD_BYTES : options.maxBytes;

  if (file.size <= 0) {
    return {
      ok: false,
      code: 'EMPTY',
      message: 'O arquivo está vazio. Selecione um documento com conteúdo.',
    };
  }

  const extension = extensionOf(file.name);
  const isPdf = extension === 'pdf';
  const imageFormat = isPdf ? null : imageFormatFor(extension);
  const isSpreadsheet = !isPdf && imageFormat === null && (SPREADSHEET_EXTENSIONS as readonly string[]).includes(extension);
  if (!isPdf && !imageFormat && !isSpreadsheet) {
    return {
      ok: false,
      code: 'UNSUPPORTED',
      message: 'Selecione um PDF, uma imagem (PNG, JPEG, TIFF, GIF, BMP, WebP…) ou uma planilha (.xlsx/.csv).',
    };
  }

  const type = (file.type ?? '').toLowerCase();
  if (isPdf && type && !['application/pdf', 'application/x-pdf', 'application/octet-stream'].includes(type)) {
    return {
      ok: false,
      code: 'MIME',
      message: 'O arquivo selecionado não é um PDF válido.',
    };
  }
  if (isSpreadsheet && type && !['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'text/csv', 'application/csv', 'text/plain', 'application/octet-stream', ''].includes(type)) {
    return {
      ok: false,
      code: 'MIME',
      message: 'O arquivo selecionado não parece ser uma planilha.',
    };
  }
  if (!isPdf && !isSpreadsheet && type && !type.startsWith('image/') && type !== 'application/octet-stream') {
    return {
      ok: false,
      code: 'MIME',
      message: 'O arquivo selecionado não parece ser uma imagem.',
    };
  }

  if (maxBytes !== null && file.size > maxBytes) {
    return {
      ok: false,
      code: 'SIZE',
      message: `O arquivo excede o limite de ${formatBytes(maxBytes)}.`,
    };
  }

  const kind = isPdf ? 'pdf' : isSpreadsheet ? 'spreadsheet' : 'image';
  return {
    ok: true,
    code: null,
    message: null,
    kind,
    format: imageFormat ?? (isSpreadsheet ? extension.toUpperCase() : null),
  };
}

/** Backwards-compatible PDF-only gate used by older screens/tests. */
export function validatePdfFile(
  file: Pick<File, 'name' | 'size' | 'type'>,
  options: FileValidationOptions = {},
): FileValidationResult {
  const result = validateDocumentFile(file, options);
  if (!result.ok) return result;
  if (result.kind !== 'pdf') {
    return {
      ok: false,
      code: 'EXTENSION',
      message: 'Selecione um arquivo com extensão .pdf.',
    };
  }
  return result;
}
