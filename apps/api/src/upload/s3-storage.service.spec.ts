import { PassThrough } from 'node:stream';

import { S3StorageService } from './s3-storage.service';

/** Structural stand-in for the parts of `@aws-sdk/client-s3` the service uses. */
function command(name: string) {
  return class {
    readonly commandName = name;
    constructor(readonly input: Record<string, unknown>) {}
  };
}

/** The managed uploader reads the body until it ends or errors, like the real one. */
class FakeUpload {
  constructor(private readonly options: { params: { Body: AsyncIterable<Buffer> } }) {}

  async done(): Promise<void> {
    for await (const _chunk of this.options.params.Body) {
      // consume
    }
  }
}

const fakeSdk = {
  S3Client: class {},
  PutObjectCommand: command('PutObjectCommand'),
  GetObjectCommand: command('GetObjectCommand'),
  DeleteObjectCommand: command('DeleteObjectCommand'),
  HeadObjectCommand: command('HeadObjectCommand'),
  CopyObjectCommand: command('CopyObjectCommand'),
  CreateMultipartUploadCommand: command('CreateMultipartUploadCommand'),
  UploadPartCommand: command('UploadPartCommand'),
  CompleteMultipartUploadCommand: command('CompleteMultipartUploadCommand'),
  AbortMultipartUploadCommand: command('AbortMultipartUploadCommand'),
  Upload: FakeUpload,
};

function s3Error(name: string, httpStatusCode: number): Error {
  return Object.assign(new Error(name), { name, $metadata: { httpStatusCode } });
}

describe('S3StorageService', () => {
  let service: S3StorageService;
  let send: jest.Mock;

  beforeEach(() => {
    service = new S3StorageService({ s3: { bucket: 'pdf2x' } } as never);
    send = jest.fn();
    const internal = service as unknown as { sdk: unknown; client: unknown };
    internal.sdk = fakeSdk;
    internal.client = { send };
  });

  describe('error mapping (A-19)', () => {
    it('maps a missing object on get to NOT_FOUND', async () => {
      send.mockRejectedValueOnce(s3Error('NoSuchKey', 404));

      await expect(service.get('documents/a.pdf')).rejects.toMatchObject({ code: 'NOT_FOUND', status: 404 });
    });

    it('maps a throttled or unreachable store on get to STORAGE_UNAVAILABLE', async () => {
      send.mockRejectedValueOnce(s3Error('SlowDown', 503));

      await expect(service.get('documents/a.pdf')).rejects.toMatchObject({
        code: 'STORAGE_UNAVAILABLE',
        status: 503,
      });
    });

    it('maps a missing object on openRead to NOT_FOUND and other failures to STORAGE_UNAVAILABLE', async () => {
      send.mockRejectedValueOnce(s3Error('NoSuchKey', 404));
      await expect(service.openRead('documents/a.pdf')).rejects.toMatchObject({ code: 'NOT_FOUND' });

      send.mockRejectedValueOnce(s3Error('InternalError', 500));
      await expect(service.openRead('documents/a.pdf')).rejects.toMatchObject({ code: 'STORAGE_UNAVAILABLE' });
    });

    it('answers exists=false only for a missing object', async () => {
      send.mockRejectedValueOnce(s3Error('NotFound', 404));
      await expect(service.exists('documents/a.pdf')).resolves.toBe(false);

      send.mockRejectedValueOnce(s3Error('ServiceUnavailable', 503));
      await expect(service.exists('documents/a.pdf')).rejects.toMatchObject({ code: 'STORAGE_UNAVAILABLE' });
    });

    it('does not report a missing bucket as a missing object', async () => {
      send.mockRejectedValueOnce(s3Error('NoSuchBucket', 404));

      await expect(service.get('documents/a.pdf')).rejects.toMatchObject({ code: 'STORAGE_UNAVAILABLE' });
    });
  });

  describe('streaming writes (A-14)', () => {
    it('stores a stream that ends normally', async () => {
      const source = new PassThrough();
      const pending = service.putStream('documents/a.pdf', source, 'application/pdf');
      source.end(Buffer.from('%PDF-1.7 body'));

      await expect(pending).resolves.toMatchObject({ key: 'documents/a.pdf', size: 13 });
    });

    it('fails the upload when the source dies mid-body instead of waiting forever', async () => {
      const source = new PassThrough();
      const pending = service.putStream('documents/b.pdf', source, 'application/pdf');
      source.write(Buffer.alloc(10, 0x25));
      setTimeout(() => source.destroy(), 10);

      await expect(pending).rejects.toMatchObject({ code: 'STORAGE_UNAVAILABLE' });
    });
  });
});
