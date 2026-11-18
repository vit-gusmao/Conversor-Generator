import {
  ConversionOptionsError,
  canonicalOptionsJson,
  defaultConversionOptions,
  normalizeConversionOptions,
  producesSpreadsheet,
} from '@pdf2x/contracts';

/**
 * Parity fixture mirrored in
 * services/converter/tests/test_options_parity.py — both sides must accept
 * and reject exactly the same shapes.
 */
describe('normalizeConversionOptions', () => {
  it('keeps the selected formats for a PDF', () => {
    expect(normalizeConversionOptions({ target_formats: ['txt'] }, { kind: 'pdf' }).target_formats)
      .toEqual(['txt']);
  });

  it('defaults a PDF to every format', () => {
    expect(normalizeConversionOptions(undefined, { kind: 'pdf' }).target_formats).toEqual([
      'xlsx',
      'txt',
      'csv',
    ]);
  });

  it('defaults an image to TXT only', () => {
    expect(normalizeConversionOptions(undefined, { kind: 'image' }).target_formats).toEqual(['txt']);
  });

  it('rejects an explicit XLSX for an image', () => {
    try {
      normalizeConversionOptions({ target_formats: ['xlsx'] }, { kind: 'image' });
      fail('expected a ConversionOptionsError');
    } catch (error) {
      expect(error).toBeInstanceOf(ConversionOptionsError);
      expect((error as ConversionOptionsError).code).toBe('INCOMPATIBLE_OUTPUT_FORMAT');
    }
  });

  it.each(['max_pages', 'maxPages', 'page_limit', 'max_pages_per_document'])(
    'rejects the page ceiling option %s',
    (key) => {
      try {
        normalizeConversionOptions({ [key]: 500 }, { kind: 'pdf' });
        fail('expected a ConversionOptionsError');
      } catch (error) {
        expect((error as ConversionOptionsError).code).toBe('INVALID_OPTION');
        expect((error as ConversionOptionsError).details).toMatchObject({ option: key });
      }
    },
  );

  it('treats a null ceiling as absent', () => {
    expect(normalizeConversionOptions({ max_pages: null }, { kind: 'pdf' }).schema_version).toBe(2);
  });

  it('accepts the legacy flat shape', () => {
    const options = normalizeConversionOptions(
      {
        target_formats: ['xlsx'],
        ocr: false,
        text_newline: 'crlf',
        page_separator: 'marker',
        csv_profile: 'excel_br',
      },
      { kind: 'pdf' },
    );
    expect(options.ocr.enabled).toBe(false);
    expect(options.text.newline).toBe('crlf');
    expect(options.text.page_separator).toBe('marker');
    expect(options.csv.profile).toBe('excel_br');
  });

  it('rejects unknown keys at both levels', () => {
    expect(() => normalizeConversionOptions({ nope: 1 }, { kind: 'pdf' })).toThrow(
      ConversionOptionsError,
    );
    expect(() => normalizeConversionOptions({ text: { invented: true } }, { kind: 'pdf' })).toThrow(
      ConversionOptionsError,
    );
  });

  it('preserves the nested v2 payload', () => {
    const options = normalizeConversionOptions(
      {
        schema_version: 2,
        target_formats: ['txt', 'csv'],
        ocr: { enabled: true, languages: ['por'], dpi: 200, strategy: 'ocr' },
        text: { newline: 'lf', page_separator: 'none', include_table_text: false },
        csv: {
          profile: 'interoperable',
          delimiter: ';',
          formula_policy: 'safe',
          table_ids: ['t1'],
          value_mode: 'canonical',
        },
      },
      { kind: 'pdf' },
    );
    expect(options.ocr).toEqual({ enabled: true, languages: ['por'], dpi: 200, strategy: 'ocr' });
    expect(options.text.include_table_text).toBe(false);
    expect(options.csv.delimiter).toBe(';');
    expect(options.csv.value_mode).toBe('canonical');
  });

  it('rejects unsupported output formats', () => {
    expect(() => normalizeConversionOptions({ target_formats: ['docx'] }, { kind: 'pdf' })).toThrow(
      ConversionOptionsError,
    );
  });

  it('canonicalises deterministically for hashing', () => {
    const a = defaultConversionOptions('pdf');
    const b = defaultConversionOptions('pdf');
    expect(canonicalOptionsJson(a)).toBe(canonicalOptionsJson(b));

    const c = { ...b, target_formats: [...b.target_formats].reverse() as typeof b.target_formats };
    // Format order is normalised, so the fingerprint is stable.
    expect(canonicalOptionsJson(c)).toBe(canonicalOptionsJson(a));
  });

  it('reports whether a spreadsheet is expected', () => {
    expect(producesSpreadsheet(defaultConversionOptions('pdf'))).toBe(true);
    expect(producesSpreadsheet(defaultConversionOptions('image'))).toBe(false);
  });
});
