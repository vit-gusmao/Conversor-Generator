import { ApiError } from '../common/errors/api-error';
import { captureApiError, makePdf } from '../../test/helpers/test-utils';

import { FileValidationService } from './file-validation.service';

describe('FileValidationService', () => {
  const service = new FileValidationService({ maxBytes: 2048, maxPages: 5 });

  it('accepts a PDF with valid magic bytes and counts pages', () => {
    const result = service.validate(makePdf(3), { filename: 'holerite.pdf' });

    expect(result.ok).toBe(true);
    expect(result.is_pdf).toBe(true);
    expect(result.pdf_version).toBe('1.7');
    expect(result.page_count).toBe(3);
    expect(result.sha256).toMatch(/^[a-f0-9]{64}$/);
    expect(result.errors).toEqual([]);
  });

  it('never mistakes /Type /Pages for a page', () => {
    expect(service.countPages('<< /Type /Pages /Count 2 >>')).toBe(0);
    expect(service.countPages('<< /Type /Page >> << /Type /Page >>')).toBe(2);
  });

  it('rejects a file that is neither PDF nor a supported image', () => {
    const error = captureApiError(() =>
      service.validate(Buffer.from('texto qualquer sem assinatura'), { filename: 'a.pdf' }),
    );

    expect(error).toBeInstanceOf(ApiError);
    expect(error.code).toBe('INVALID_FILE');
    expect(error.status).toBe(400);
  });

  it('accepts an image by content and declares the detected format', () => {
    const png = Buffer.concat([
      Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
      Buffer.alloc(64, 0),
    ]);
    const result = service.validate(png, { filename: 'foto.pdf' });

    expect(result.ok).toBe(true);
    expect(result.is_pdf).toBe(false);
    expect(result.kind).toBe('image');
    expect(result.image_format).toBe('PNG');
    expect(result.mime_type).toBe('image/png');
  });

  it('rejects an oversized image', () => {
    const gif = Buffer.concat([
      Buffer.from('GIF89a'),
      Buffer.alloc(4096, 0x20),
    ]);
    const error = captureApiError(() => service.validate(gif, { filename: 'anim.gif' }));

    expect(error.code).toBe('FILE_TOO_LARGE');
  });

  it('rejects an empty file', () => {
    const error = captureApiError(() => service.validate(Buffer.alloc(0), { filename: 'vazio.pdf' }));

    expect(error.code).toBe('INVALID_FILE');
    expect(error.details).toMatchObject({ reason: 'empty_file' });
  });

  it('rejects a file above the size limit', () => {
    const oversized = Buffer.concat([Buffer.from('%PDF-1.7\n'), Buffer.alloc(4096, 0x20)]);
    const error = captureApiError(() => service.validate(oversized, { filename: 'grande.pdf' }));

    expect(error.code).toBe('FILE_TOO_LARGE');
    expect(error.details).toMatchObject({ max_bytes: 2048 });
  });

  it('rejects a PDF with too many pages', () => {
    const error = captureApiError(() => service.validate(makePdf(6), { filename: 'longo.pdf' }));

    expect(error.code).toBe('TOO_MANY_PAGES');
    expect(error.details).toMatchObject({ max_pages: 5, page_count: 6 });
  });

  it('flags encrypted PDFs but still accepts them', () => {
    const encrypted = Buffer.concat([makePdf(1), Buffer.from('/Encrypt 10 0 R\n')]);
    const result = service.validate(encrypted, { filename: 'protegido.pdf' });

    expect(result.encrypted).toBe(true);
    expect(result.needs_password).toBe(true);
    expect(result.warnings).toContain('encrypted_pdf');
  });

  it('reports when the page count cannot be estimated', () => {
    const objectStream = Buffer.from('%PDF-1.7\n1 0 obj\n<< /Type /ObjStm >>\nendobj\n%%EOF\n');
    const result = service.validate(objectStream);

    expect(result.page_count).toBe(0);
    expect(result.warnings).toContain('page_count_unavailable');
  });
});
