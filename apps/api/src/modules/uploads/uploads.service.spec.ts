import { createHash } from 'node:crypto';
import { PassThrough, Readable } from 'node:stream';

import { makePdf } from '../../../test/helpers/test-utils';
import {
  createHarness,
  listFiles,
  OWNER,
  pngBytes,
  registerUpload,
  uploadAndComplete,
  type Harness,
} from '../../../test/helpers/batch-harness';
import { uploadPartKey } from '../../upload/storage.service';

const sha = (bytes: Buffer): string => createHash('sha256').update(bytes).digest('hex');

/** Sends every part without completing, so a test can intervene before `complete`. */
async function sendParts(h: Harness, uploadId: string, bytes: Buffer, partSize: number): Promise<number> {
  const totalParts = Math.ceil(bytes.length / partSize);
  for (let part = 0; part < totalParts; part += 1) {
    const chunk = bytes.subarray(part * partSize, (part + 1) * partSize);
    await h.uploads.putPart(OWNER, uploadId, part, Readable.from([chunk]), sha(chunk), chunk.length);
  }
  return totalParts;
}

describe('UploadsService', () => {
  let h: Harness;

  beforeEach(() => {
    h = createHarness(64);
  });

  afterEach(() => {
    jest.restoreAllMocks();
    h.dispose();
  });

  describe('part size cap (A-01)', () => {
    it('refuses an oversized chunked part as soon as it exceeds the negotiated size', async () => {
      const batch = await h.batches.create(OWNER, 'cap');
      const { uploadId } = await registerUpload(h, batch.batch_id, 'big', 'big.pdf', Buffer.alloc(1000, 0x20));

      // No Content-Length: the size is only known while streaming. 125 chunks of
      // 8 bytes is 1000 bytes; the source must stop being read near the 64-byte cap.
      let pulled = 0;
      const source = Readable.from(
        (function* () {
          for (let index = 0; index < 125; index += 1) {
            pulled += 8;
            yield Buffer.alloc(8, 0x20);
          }
        })(),
      );

      await expect(h.uploads.putPart(OWNER, uploadId, 0, source, undefined, null)).rejects.toMatchObject({
        code: 'FILE_TOO_LARGE',
        status: 400,
      });
      expect(pulled).toBeLessThan(400);
      expect(listFiles(h.storageRoot).filter((file) => file.endsWith('.part'))).toEqual([]);
      expect(h.prisma.parts).toHaveLength(0);
    });
  });

  describe('client disconnect (A-14)', () => {
    it('fails the part and leaves no partial object when the client disconnects mid-part', async () => {
      const batch = await h.batches.create(OWNER, 'abort');
      const { uploadId } = await registerUpload(h, batch.batch_id, 'drop', 'drop.pdf', Buffer.alloc(200, 0x20));

      const source = new PassThrough();
      const pending = h.uploads.putPart(OWNER, uploadId, 0, source, undefined, null);
      source.write(Buffer.alloc(10, 0x20));
      setTimeout(() => source.destroy(), 20);

      await expect(pending).rejects.toMatchObject({ code: 'STORAGE_UNAVAILABLE' });
      expect(listFiles(h.storageRoot).filter((file) => file.endsWith('.part'))).toEqual([]);
      expect(h.prisma.parts).toHaveLength(0);
    });
  });

  describe('duplicate part race (A-04)', () => {
    it('keeps the confirmed part when a divergent writer loses the race', async () => {
      const winnerBytes = Buffer.from('winner-bytes-0001');
      const batch = await h.batches.create(OWNER, 'race-divergent');
      const { uploadId } = await registerUpload(h, batch.batch_id, 'race', 'race.pdf', winnerBytes);
      const winnerKey = uploadPartKey(uploadId, 0);
      await h.storage.put(winnerKey, winnerBytes);

      // The winner's row lands between this request's check and its insert.
      h.prisma.uploadPart.create = async () => {
        h.prisma.parts.push({
          uploadId,
          partNumber: 0,
          size: winnerBytes.length,
          sha256: sha(winnerBytes),
          storageKey: winnerKey,
        });
        throw Object.assign(new Error('Unique constraint failed'), { code: 'P2002' });
      };

      await expect(
        h.uploads.putPart(OWNER, uploadId, 0, Readable.from([Buffer.from('XXXXXXXXXXXXXXXXX')]), undefined, null),
      ).rejects.toMatchObject({ code: 'UPLOAD_PART_MISMATCH', status: 409 });

      expect((await h.storage.get(winnerKey)).toString()).toBe('winner-bytes-0001');
      expect(listFiles(h.storageRoot).filter((file) => file.includes('incoming'))).toEqual([]);
    });

    it('replays an identical racing part and leaves the stored object intact', async () => {
      const bytes = Buffer.from('same-bytes-000001');
      const batch = await h.batches.create(OWNER, 'race-same');
      const { uploadId } = await registerUpload(h, batch.batch_id, 'race', 'race.pdf', bytes);
      const winnerKey = uploadPartKey(uploadId, 0);
      await h.storage.put(winnerKey, bytes);

      h.prisma.uploadPart.create = async () => {
        h.prisma.parts.push({
          uploadId,
          partNumber: 0,
          size: bytes.length,
          sha256: sha(bytes),
          storageKey: winnerKey,
        });
        throw Object.assign(new Error('Unique constraint failed'), { code: 'P2002' });
      };

      const receipt = await h.uploads.putPart(OWNER, uploadId, 0, Readable.from([bytes]), undefined, null);

      expect(receipt.replayed).toBe(true);
      expect((await h.storage.get(winnerKey)).toString()).toBe('same-bytes-000001');
    });
  });

  describe('cancellation (A-05)', () => {
    it('refuses to complete an upload once its batch is cancelled', async () => {
      const bytes = makePdf(2);
      const batch = await h.batches.create(OWNER, 'cancel-complete');
      const { uploadId, partSize } = await registerUpload(h, batch.batch_id, 'doc', 'a.pdf', bytes);
      const totalParts = await sendParts(h, uploadId, bytes, partSize);

      await h.batches.cancel(OWNER, batch.batch_id);

      await expect(h.uploads.complete(OWNER, uploadId, sha(bytes), totalParts)).rejects.toMatchObject({
        code: 'BATCH_CANCELLED',
        status: 409,
      });
      expect(h.conversions.createStoredConversion).not.toHaveBeenCalled();
    });

    it('refuses to complete an upload whose item was cancelled', async () => {
      const bytes = makePdf(2);
      const batch = await h.batches.create(OWNER, 'cancel-item');
      const { uploadId, itemId, partSize } = await registerUpload(h, batch.batch_id, 'doc', 'a.pdf', bytes);
      const totalParts = await sendParts(h, uploadId, bytes, partSize);

      await h.batches.cancelItem(OWNER, batch.batch_id, itemId);

      await expect(h.uploads.complete(OWNER, uploadId, sha(bytes), totalParts)).rejects.toMatchObject({
        code: 'UPLOAD_STATE_CONFLICT',
        details: { reason: 'item_cancelled' },
      });
      expect(h.conversions.createStoredConversion).not.toHaveBeenCalled();
    });

    it('refuses new bytes for an item of a cancelled batch', async () => {
      const bytes = Buffer.alloc(100, 0x20);
      const batch = await h.batches.create(OWNER, 'cancel-put');
      const { uploadId } = await registerUpload(h, batch.batch_id, 'doc', 'a.pdf', bytes);
      await h.batches.cancel(OWNER, batch.batch_id);

      await expect(
        h.uploads.putPart(OWNER, uploadId, 0, Readable.from([bytes.subarray(0, 64)]), undefined, null),
      ).rejects.toMatchObject({ code: 'BATCH_CANCELLED' });
    });
  });

  describe('expiry (S-04)', () => {
    it('returns UPLOAD_EXPIRED from complete, before the completeness check', async () => {
      const bytes = makePdf(1);
      const batch = await h.batches.create(OWNER, 'expired');
      const { uploadId } = await registerUpload(h, batch.batch_id, 'doc', 'a.pdf', bytes);
      h.prisma.uploads[0]['expiresAt'] = new Date(Date.now() - 60_000);

      await expect(h.uploads.complete(OWNER, uploadId, 'a'.repeat(64), 0)).rejects.toMatchObject({
        code: 'UPLOAD_EXPIRED',
        status: 409,
      });
    });
  });

  describe('options rejected at assembly (A-16)', () => {
    it('answers 400 with the options code, marks the item upload_failed, and stores nothing', async () => {
      // Declared as a PDF at registration, but the bytes are a PNG: the options
      // that were valid for a PDF are refused for an image at assembly.
      const bytes = pngBytes(80);
      const batch = await h.batches.create(OWNER, 'options-error');
      const { uploadId, itemId, partSize } = await registerUpload(h, batch.batch_id, 'scan', 'scan.pdf', bytes, {
        target_formats: ['xlsx'],
      });

      await expect(uploadAndComplete(h, uploadId, bytes, partSize)).rejects.toMatchObject({
        code: 'INCOMPATIBLE_OUTPUT_FORMAT',
        status: 400,
      });

      const item = h.prisma.items.find((row) => row['id'] === itemId);
      expect(item?.['status']).toBe('upload_failed');
      expect(item?.['errorJson']).toContain('INCOMPATIBLE_OUTPUT_FORMAT');
      expect(h.conversions.createStoredConversion).not.toHaveBeenCalled();
      expect(listFiles(h.storageRoot).filter((file) => file.startsWith('documents'))).toEqual([]);
    });
  });

  describe('rejected content marks the item upload_failed', () => {
    it('marks the item and stores nothing when the bytes have no known signature', async () => {
      const bytes = Buffer.from('plain text, not a document at all\n'.repeat(4));
      const batch = await h.batches.create(OWNER, 'unknown-magic');
      const { uploadId, itemId, partSize } = await registerUpload(h, batch.batch_id, 'junk', 'junk.pdf', bytes);
      await sendParts(h, uploadId, bytes, partSize);

      await expect(
        h.uploads.complete(OWNER, uploadId, sha(bytes), Math.ceil(bytes.length / partSize)),
      ).rejects.toMatchObject({ code: 'INVALID_FILE', status: 400 });

      const item = h.prisma.items.find((row) => row['id'] === itemId);
      expect(item?.['status']).toBe('upload_failed');
      expect(item?.['errorJson']).toContain('INVALID_FILE');
      expect(h.conversions.createStoredConversion).not.toHaveBeenCalled();
      expect(listFiles(h.storageRoot).filter((file) => file.startsWith('documents'))).toEqual([]);
    });

    it('marks the item when the assembled checksum does not match the declared sha256', async () => {
      const bytes = makePdf(2);
      const batch = await h.batches.create(OWNER, 'checksum-mismatch');
      const { uploadId, itemId, partSize } = await registerUpload(h, batch.batch_id, 'sum', 'sum.pdf', bytes);
      await sendParts(h, uploadId, bytes, partSize);

      await expect(
        h.uploads.complete(OWNER, uploadId, sha(Buffer.from('other bytes')), Math.ceil(bytes.length / partSize)),
      ).rejects.toMatchObject({ status: 422 });

      const item = h.prisma.items.find((row) => row['id'] === itemId);
      expect(item?.['status']).toBe('upload_failed');
      expect(item?.['errorJson']).toContain('UPLOAD_CHECKSUM_MISMATCH');
      expect(h.conversions.createStoredConversion).not.toHaveBeenCalled();
    });
  });

  describe('concurrent complete (A-21)', () => {
    it('replays the conversion when a concurrent complete moved the document first', async () => {
      const bytes = pngBytes(80);
      const batch = await h.batches.create(OWNER, 'concurrent');
      const { uploadId, itemId, partSize } = await registerUpload(h, batch.batch_id, 'img', 'img.png', bytes);
      await sendParts(h, uploadId, bytes, partSize);

      // The other request wins the move and records its conversion; this one
      // finds the staging object already gone.
      jest.spyOn(h.storage, 'move').mockImplementationOnce(async () => {
        const item = h.prisma.items.find((row) => row['id'] === itemId);
        if (item) item['conversionId'] = 'conv-other';
        throw Object.assign(new Error('ENOENT: no such file or directory'), { code: 'ENOENT' });
      });

      const response = await h.uploads.complete(OWNER, uploadId, sha(bytes), Math.ceil(bytes.length / partSize));

      expect(response).toMatchObject({ conversion_id: 'conv-other', replayed: true });
      expect(h.conversions.createStoredConversion).not.toHaveBeenCalled();
    });
  });
});
