import { makePdf } from '../../../test/helpers/test-utils';
import {
  createHarness,
  OWNER,
  registerUpload,
  uploadAndComplete,
  type Harness,
} from '../../../test/helpers/batch-harness';

/** Completes one item end to end and returns its conversion id. */
async function completedItem(h: Harness, batchId: string, clientItemId: string) {
  const bytes = makePdf(2);
  const { uploadId, itemId, partSize } = await registerUpload(h, batchId, clientItemId, `${clientItemId}.pdf`, bytes);
  const response = await uploadAndComplete(h, uploadId, bytes, partSize);
  return { uploadId, itemId, conversionId: response.conversion_id, bytes, partSize };
}

/** Simulates the engine finishing a conversion (the stored item stays "queued"). */
function setConversionStatus(h: Harness, conversionId: string, status: string): void {
  const row = h.prisma.conversions.find((candidate) => candidate['id'] === conversionId);
  if (!row) throw new Error(`conversion ${conversionId} not found`);
  row['status'] = status;
}

describe('BatchesService', () => {
  let h: Harness;

  beforeEach(() => {
    h = createHarness(64);
  });

  afterEach(() => {
    h.dispose();
  });

  describe('terminal guards use the conversion status (A-06)', () => {
    it('cancel does not overwrite an item whose conversion already completed', async () => {
      const batch = await h.batches.create(OWNER, 'cancel-finished');
      const done = await completedItem(h, batch.batch_id, 'done');
      setConversionStatus(h, done.conversionId, 'completed');

      const cancelled = await h.batches.cancel(OWNER, batch.batch_id);
      const page = await h.batches.listItems(OWNER, batch.batch_id, {});

      expect(h.conversions.cancel).not.toHaveBeenCalledWith(OWNER, done.conversionId);
      expect(page.items[0].status).toBe('completed');
      expect(cancelled.counts.completed).toBe(1);
      expect(cancelled.counts.cancelled).toBe(0);
    });

    it('cancelItem returns a finished item unchanged', async () => {
      const batch = await h.batches.create(OWNER, 'cancel-item-finished');
      const done = await completedItem(h, batch.batch_id, 'done');
      setConversionStatus(h, done.conversionId, 'completed_with_warnings');

      const item = await h.batches.cancelItem(OWNER, batch.batch_id, done.itemId);

      expect(item.status).toBe('completed_with_warnings');
      expect(h.conversions.cancel).not.toHaveBeenCalled();
    });

    it('retry of a conversion on a finished item is accepted', async () => {
      const batch = await h.batches.create(OWNER, 'retry-finished');
      const done = await completedItem(h, batch.batch_id, 'done');
      setConversionStatus(h, done.conversionId, 'failed');

      const result = await h.batches.retryItem(OWNER, batch.batch_id, done.itemId, 'conversion');

      expect(h.conversions.retry).toHaveBeenCalledWith(OWNER, done.conversionId);
      expect(result.item.status).toBe('queued');
    });

    it('retry of a conversion still running is refused', async () => {
      const batch = await h.batches.create(OWNER, 'retry-busy');
      const running = await completedItem(h, batch.batch_id, 'running');
      setConversionStatus(h, running.conversionId, 'extracting');

      await expect(h.batches.retryItem(OWNER, batch.batch_id, running.itemId, 'conversion')).rejects.toMatchObject({
        code: 'CONVERSION_BUSY',
      });
      expect(h.conversions.retry).not.toHaveBeenCalled();
    });
  });

  describe('upload retry scope (A-07)', () => {
    it('refuses to resume the upload of an item whose upload finished', async () => {
      const batch = await h.batches.create(OWNER, 'upload-retry-finished');
      const done = await completedItem(h, batch.batch_id, 'done');

      await expect(h.batches.retryItem(OWNER, batch.batch_id, done.itemId, 'upload')).rejects.toMatchObject({
        code: 'UPLOAD_STATE_CONFLICT',
        status: 409,
      });
    });

    it('resumes the upload of an item that failed at upload', async () => {
      const batch = await h.batches.create(OWNER, 'upload-retry-failed');
      const bytes = makePdf(2);
      const { itemId } = await registerUpload(h, batch.batch_id, 'failed', 'failed.pdf', bytes);
      await h.batches.markItemUploadFailed(batch.batch_id, itemId, { code: 'X', message: 'x' });

      const result = await h.batches.retryItem(OWNER, batch.batch_id, itemId, 'upload');

      expect(result.item.status).toBe('uploading');
    });

    it('refuses any retry once the batch is cancelled', async () => {
      const batch = await h.batches.create(OWNER, 'retry-cancelled');
      const bytes = makePdf(2);
      const { itemId } = await registerUpload(h, batch.batch_id, 'x', 'x.pdf', bytes);
      await h.batches.cancel(OWNER, batch.batch_id);

      await expect(h.batches.retryItem(OWNER, batch.batch_id, itemId, 'upload')).rejects.toMatchObject({
        code: 'BATCH_CANCELLED',
      });
    });
  });

  describe('upload_failed roll-up (A-08)', () => {
    it('counts upload_failed as failed and lets the batch reach a terminal status', async () => {
      const batch = await h.batches.create(OWNER, 'rollup');
      const bytes = makePdf(2);
      const abandoned = await registerUpload(h, batch.batch_id, 'abandoned', 'a.pdf', bytes);
      await h.uploads.abort(OWNER, abandoned.uploadId);
      const done = await completedItem(h, batch.batch_id, 'done');
      setConversionStatus(h, done.conversionId, 'completed');

      const sealed = await h.batches.seal(OWNER, batch.batch_id);
      const summary = await h.batches.get(OWNER, batch.batch_id);

      expect(summary.counts.pending_upload).toBe(0);
      expect(summary.counts.failed).toBe(1);
      expect(summary.counts.completed).toBe(1);
      expect(summary.status).toBe('completed_with_errors');
      expect(sealed.status).toBe('completed_with_errors');
    });

    it('does not block sealing on an upload_failed item', async () => {
      const batch = await h.batches.create(OWNER, 'seal-failed');
      const bytes = makePdf(2);
      const { itemId } = await registerUpload(h, batch.batch_id, 'x', 'x.pdf', bytes);
      await h.batches.markItemUploadFailed(batch.batch_id, itemId, { code: 'X', message: 'x' });

      await expect(h.batches.seal(OWNER, batch.batch_id)).resolves.toMatchObject({ message: 'Lote selado.' });
    });
  });

  describe('conversion terminal propagation (A-09)', () => {
    it('records the item outcome, reaches a terminal batch status and streams the change', async () => {
      const batch = await h.batches.create(OWNER, 'terminal');
      const done = await completedItem(h, batch.batch_id, 'done');
      await h.batches.seal(OWNER, batch.batch_id);

      const received: Array<{ type: string; status: string; item_status: string | null }> = [];
      const subscription = h.events.stream(batch.batch_id).subscribe((event) => {
        received.push({ type: event.type, status: event.status, item_status: event.item_status });
      });

      setConversionStatus(h, done.conversionId, 'completed');
      await h.batches.onConversionTerminal(done.conversionId);
      subscription.unsubscribe();

      const summary = await h.batches.get(OWNER, batch.batch_id);
      expect(summary.status).toBe('completed');
      expect(received).toContainEqual(expect.objectContaining({ type: 'item', item_status: 'completed' }));
      expect(received).toContainEqual(expect.objectContaining({ type: 'batch', status: 'completed' }));
    });
  });

  describe('markItemQueued (A-05)', () => {
    it('does not overwrite a cancelled item', async () => {
      const batch = await h.batches.create(OWNER, 'queued-cancelled');
      const bytes = makePdf(2);
      const { itemId } = await registerUpload(h, batch.batch_id, 'x', 'x.pdf', bytes);
      await h.batches.cancelItem(OWNER, batch.batch_id, itemId);

      const queued = await h.batches.markItemQueued(batch.batch_id, itemId, 'conv-late', null);

      expect(queued).toBe(false);
      const stored = h.prisma.items.find((row) => row['id'] === itemId);
      expect(stored?.['status']).toBe('cancelled');
      expect(stored?.['conversionId']).toBeFalsy();
    });
  });

  describe('item lookup and pagination', () => {
    it('finds an item by client key beyond the first 200 items', async () => {
      const batch = await h.batches.create(OWNER, 'lookup');
      const bytes = makePdf(1);
      for (let index = 0; index < 250; index += 1) {
        await registerUpload(h, batch.batch_id, `item-${String(index).padStart(3, '0')}`, `d${index}.pdf`, bytes, {
          target_formats: ['txt'],
        });
      }

      const found = await h.batches.findItemByClientId(OWNER, batch.batch_id, 'item-240');
      const missing = await h.batches.findItemByClientId(OWNER, batch.batch_id, 'item-999');

      expect(found?.client_item_id).toBe('item-240');
      expect(found?.options).toMatchObject({ target_formats: ['txt'] });
      expect(missing).toBeNull();
    });

    it('fills status-filtered pages and keeps the cursor consistent (A-20)', async () => {
      const batch = await h.batches.create(OWNER, 'filter-pages');
      const bytes = makePdf(1);
      const ids: string[] = [];
      for (let index = 0; index < 10; index += 1) {
        const { itemId } = await registerUpload(h, batch.batch_id, `i-${index}`, `d${index}.pdf`, bytes);
        ids.push(itemId);
        if (index % 2 === 1) {
          // Odd items are cancelled: the filter must find all five of them.
          const row = h.prisma.items.find((candidate) => candidate['id'] === itemId);
          if (row) row['status'] = 'cancelled';
        }
      }
      const expected = ids.filter((_, index) => index % 2 === 1);

      const seen: string[] = [];
      let cursor: string | null = null;
      const pageSizes: number[] = [];
      do {
        const page = await h.batches.listItems(OWNER, batch.batch_id, {
          limit: 2,
          status: 'cancelled',
          cursor,
        });
        pageSizes.push(page.items.length);
        expect(page.items.every((item) => item.status === 'cancelled')).toBe(true);
        seen.push(...page.items.map((item) => item.item_id));
        cursor = page.next_cursor;
      } while (cursor);

      expect(seen).toEqual(expected);
      expect(pageSizes).toEqual([2, 2, 1]);
    });
  });
});
