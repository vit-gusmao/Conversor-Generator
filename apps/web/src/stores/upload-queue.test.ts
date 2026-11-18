import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { defaultConversionOptions } from '@pdf2x/contracts';
import { useUploadQueue } from '@/stores/upload-queue';

const PDF_OPTIONS = defaultConversionOptions('pdf');

function pdf(name: string, content = '%PDF-1.4'): File {
  return new File([content], name, { type: 'application/pdf' });
}

function image(name: string, content = 'png'): File {
  return new File([content], name, { type: 'image/png' });
}

beforeEach(() => {
  useUploadQueue.getState().reset();
});

afterEach(() => {
  useUploadQueue.getState().reset();
});

describe('useUploadQueue', () => {
  it('appends 1.001 items without dropping any', () => {
    const store = useUploadQueue.getState();
    store.enqueueFiles([pdf('primeiro.pdf')], PDF_OPTIONS);

    const many = Array.from({ length: 1000 }, (_, index) =>
      pdf(`documento-${index}.pdf`, `%PDF-1.4 ${index}`),
    );
    useUploadQueue.getState().enqueueFiles(many, PDF_OPTIONS);

    const items = useUploadQueue.getState().items;
    expect(items).toHaveLength(1001);
    expect(new Set(items.map((item) => item.clientItemId)).size).toBe(1001);
  });

  it('gives an image TXT only while a PDF keeps xlsx/txt/csv', () => {
    useUploadQueue.getState().enqueueFiles([pdf('relatorio.pdf'), image('foto.png')], PDF_OPTIONS);

    const items = useUploadQueue.getState().items;
    const pdfItem = items.find((item) => item.filename === 'relatorio.pdf');
    const imageItem = items.find((item) => item.filename === 'foto.png');

    expect(pdfItem?.kind).toBe('pdf');
    expect(pdfItem?.options.target_formats).toEqual(['xlsx', 'txt', 'csv']);
    expect(imageItem?.kind).toBe('image');
    expect(imageItem?.options.target_formats).toEqual(['txt']);
  });

  it('keeps same-name files with different sizes as distinct items', () => {
    useUploadQueue
      .getState()
      .enqueueFiles(
        [pdf('contrato.pdf', 'a'), pdf('contrato.pdf', 'conteudo bem maior')],
        PDF_OPTIONS,
      );

    const items = useUploadQueue.getState().items;
    expect(items).toHaveLength(2);
    expect(items[0]?.size).not.toBe(items[1]?.size);
    expect(items[0]?.clientItemId).not.toBe(items[1]?.clientItemId);
  });

  it('removes only the selected item', () => {
    useUploadQueue
      .getState()
      .enqueueFiles([pdf('a.pdf', 'a'), pdf('b.pdf', 'bb'), pdf('c.pdf', 'ccc')], PDF_OPTIONS);

    const target = useUploadQueue.getState().items[1];
    expect(target).toBeDefined();
    useUploadQueue.getState().removeItem(target!.clientItemId);

    const items = useUploadQueue.getState().items;
    expect(items.map((item) => item.filename)).toEqual(['a.pdf', 'c.pdf']);
  });

  it('reset clears every item and its metadata', () => {
    useUploadQueue.getState().enqueueFiles([pdf('a.pdf'), image('b.png')], PDF_OPTIONS);
    useUploadQueue.getState().setItemState(useUploadQueue.getState().items[0]!.clientItemId, {
      state: 'uploading',
      uploadId: 'upload-1',
      uploadedBytes: 12,
    });

    useUploadQueue.getState().reset();

    expect(useUploadQueue.getState().items).toEqual([]);
    expect(useUploadQueue.getState().hydrated).toBe(true);
  });
});
