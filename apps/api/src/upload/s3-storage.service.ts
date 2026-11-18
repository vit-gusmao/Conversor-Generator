import { createHash } from 'node:crypto';
import { Readable, Transform } from 'node:stream';

import { Injectable, Logger } from '@nestjs/common';

import { ApiError } from '../common/errors/api-error';
import { AppConfigService } from '../config/app-config.service';

import type { StorageService, StoredObject } from './storage.service';

/**
 * Minimal structural view of the pieces we use from `@aws-sdk/client-s3`.
 * The SDK is loaded lazily so the API boots (and tests pass) without it.
 */
interface S3Command {
  readonly commandName?: string;
}

interface S3ClientLike {
  send(command: S3Command): Promise<unknown>;
}

interface S3ModuleLike {
  S3Client: new (config: Record<string, unknown>) => S3ClientLike;
  PutObjectCommand: new (input: Record<string, unknown>) => S3Command;
  GetObjectCommand: new (input: Record<string, unknown>) => S3Command;
  DeleteObjectCommand: new (input: Record<string, unknown>) => S3Command;
  HeadObjectCommand: new (input: Record<string, unknown>) => S3Command;
  CopyObjectCommand: new (input: Record<string, unknown>) => S3Command;
  CreateMultipartUploadCommand: new (input: Record<string, unknown>) => S3Command;
  UploadPartCommand: new (input: Record<string, unknown>) => S3Command;
  CompleteMultipartUploadCommand: new (input: Record<string, unknown>) => S3Command;
  AbortMultipartUploadCommand: new (input: Record<string, unknown>) => S3Command;
  Upload: new (input: Record<string, unknown>) => { done(): Promise<unknown> };
}

interface S3BodyLike {
  transformToByteArray?: () => Promise<Uint8Array>;
  [Symbol.asyncIterator]?: () => AsyncIterator<Uint8Array>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

@Injectable()
export class S3StorageService implements StorageService {
  readonly backend = 's3' as const;
  private readonly logger = new Logger(S3StorageService.name);
  private readonly config: AppConfigService;
  private sdk: S3ModuleLike | null = null;
  private client: S3ClientLike | null = null;

  constructor(config: AppConfigService) {
    this.config = config;
  }

  async put(key: string, data: Buffer, contentType: string): Promise<StoredObject> {
    const sdk = this.loadSdk();
    const client = this.getClient(sdk);
    try {
      await client.send(
        new sdk.PutObjectCommand({
          Bucket: this.config.s3.bucket,
          Key: key,
          Body: data,
          ContentType: contentType,
          ContentLength: data.length,
        }),
      );
      return { key, size: data.length };
    } catch (error) {
      this.logger.error(`Falha ao enviar ${key} para o S3`, String(error));
      throw ApiError.storageUnavailable({ backend: 's3', operation: 'put' });
    }
  }

  async get(key: string): Promise<Buffer> {
    const sdk = this.loadSdk();
    const client = this.getClient(sdk);
    try {
      const output = await client.send(
        new sdk.GetObjectCommand({ Bucket: this.config.s3.bucket, Key: key }),
      );
      return await this.readBody(output);
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw ApiError.notFound({ reason: 'storage_object_not_found', key });
    }
  }

  async delete(key: string): Promise<void> {
    const sdk = this.loadSdk();
    const client = this.getClient(sdk);
    try {
      await client.send(
        new sdk.DeleteObjectCommand({ Bucket: this.config.s3.bucket, Key: key }),
      );
    } catch (error) {
      this.logger.warn(`Falha ao remover ${key} do S3: ${String(error)}`);
    }
  }

  async exists(key: string): Promise<boolean> {
    const sdk = this.loadSdk();
    const client = this.getClient(sdk);
    try {
      await client.send(
        new sdk.HeadObjectCommand({ Bucket: this.config.s3.bucket, Key: key }),
      );
      return true;
    } catch {
      return false;
    }
  }

  /** Streams the object to S3 without buffering the whole file. */
  async putStream(key: string, stream: Readable, contentType: string): Promise<StoredObject> {
    const sdk = this.loadSdk();
    const hash = createHash('sha256');
    let size = 0;
    const counter = new Transform({
      transform(chunk: Buffer, _encoding, callback) {
        hash.update(chunk);
        size += chunk.length;
        callback(null, chunk);
      },
    });
    const passthrough = stream.pipe(counter);
    try {
      await new sdk.Upload({
        client: this.getClient(sdk),
        params: { Bucket: this.config.s3.bucket, Key: key, Body: passthrough, ContentType: contentType },
      }).done();
      return { key, size };
    } catch (error) {
      this.logger.error(`Falha ao enviar ${key} para o S3`, String(error));
      throw ApiError.storageUnavailable({ backend: 's3', operation: 'put_stream' });
    }
  }

  async openRead(key: string): Promise<Readable> {
    const sdk = this.loadSdk();
    const client = this.getClient(sdk);
    const output = await client.send(
      new sdk.GetObjectCommand({ Bucket: this.config.s3.bucket, Key: key }),
    );
    if (!isRecord(output) || !isRecord(output['Body'])) {
      throw ApiError.notFound({ reason: 'storage_object_not_found', key });
    }
    const body = output['Body'] as S3BodyLike;
    if (typeof body[Symbol.asyncIterator] === 'function') {
      return Readable.from(body as AsyncIterable<Uint8Array>);
    }
    return Readable.from([await this.readBody(output)]);
  }

  async size(key: string): Promise<number> {
    const sdk = this.loadSdk();
    const client = this.getClient(sdk);
    try {
      const output = await client.send(
        new sdk.HeadObjectCommand({ Bucket: this.config.s3.bucket, Key: key }),
      );
      const length = isRecord(output) ? output['ContentLength'] : undefined;
      return typeof length === 'number' ? length : -1;
    } catch {
      return -1;
    }
  }

  async move(sourceKey: string, targetKey: string): Promise<void> {
    const sdk = this.loadSdk();
    const client = this.getClient(sdk);
    await client.send(
      new sdk.CopyObjectCommand({
        Bucket: this.config.s3.bucket,
        Key: targetKey,
        CopySource: `/${this.config.s3.bucket}/${sourceKey}`,
      }),
    );
    await this.delete(sourceKey);
  }

  /**
   * Assembles the confirmed parts server-side with a multipart upload.
   * Memory stays bounded by the part size, not the document size.
   */
  async compose(partKeys: string[], targetKey: string, contentType: string): Promise<StoredObject> {
    const sdk = this.loadSdk();
    const client = this.getClient(sdk);
    const created = await client.send(
      new sdk.CreateMultipartUploadCommand({
        Bucket: this.config.s3.bucket,
        Key: targetKey,
        ContentType: contentType,
      }),
    );
    const uploadId = isRecord(created) ? created['UploadId'] : undefined;
    if (typeof uploadId !== 'string') {
      throw ApiError.storageUnavailable({ reason: 'multipart_create_failed' });
    }

    let size = 0;
    const completed: Array<{ PartNumber: number; ETag: string }> = [];
    try {
      let index = 0;
      for (const key of partKeys) {
        index += 1;
        const body = await this.get(key);
        size += body.length;
        const sent = await client.send(
          new sdk.UploadPartCommand({
            Bucket: this.config.s3.bucket,
            Key: targetKey,
            UploadId: uploadId,
            PartNumber: index,
            Body: body,
          }),
        );
        const etag = isRecord(sent) ? sent['ETag'] : undefined;
        if (typeof etag !== 'string') throw new Error('missing ETag');
        completed.push({ PartNumber: index, ETag: etag });
      }
      await client.send(
        new sdk.CompleteMultipartUploadCommand({
          Bucket: this.config.s3.bucket,
          Key: targetKey,
          UploadId: uploadId,
          MultipartUpload: { Parts: completed },
        }),
      );
    } catch (error) {
      await client
        .send(
          new sdk.AbortMultipartUploadCommand({
            Bucket: this.config.s3.bucket,
            Key: targetKey,
            UploadId: uploadId,
          }),
        )
        .catch(() => undefined);
      throw error;
    }
    return { key: targetKey, size };
  }

  private getClient(sdk: S3ModuleLike): S3ClientLike {
    if (!this.client) {
      const s3 = this.config.s3;
      this.client = new sdk.S3Client({
        region: s3.region,
        ...(s3.endpoint ? { endpoint: s3.endpoint } : {}),
        forcePathStyle: s3.forcePathStyle,
        ...(s3.accessKeyId && s3.secretAccessKey
          ? { credentials: { accessKeyId: s3.accessKeyId, secretAccessKey: s3.secretAccessKey } }
          : {}),
      });
    }
    return this.client;
  }

  private async readBody(output: unknown): Promise<Buffer> {
    if (!isRecord(output)) throw ApiError.storageUnavailable({ reason: 'empty_s3_response' });
    const body = output['Body'];
    if (body instanceof Uint8Array) return Buffer.from(body);
    if (!isRecord(body)) throw ApiError.storageUnavailable({ reason: 'unreadable_s3_body' });

    const typed = body as S3BodyLike;
    if (typeof typed.transformToByteArray === 'function') {
      return Buffer.from(await typed.transformToByteArray());
    }
    const asyncIterator = typed[Symbol.asyncIterator];
    if (typeof asyncIterator === 'function') {
      const chunks: Buffer[] = [];
      const iterator = asyncIterator.call(body);
      for (;;) {
        const next = await iterator.next();
        if (next.done) break;
        chunks.push(Buffer.from(next.value));
      }
      return Buffer.concat(chunks);
    }
    throw ApiError.storageUnavailable({ reason: 'unreadable_s3_body' });
  }

  /** `require` keeps the SDK optional: the app never statically imports it. */
  private loadSdk(): S3ModuleLike {
    if (this.sdk) return this.sdk;
    try {
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      const loaded: unknown = require('@aws-sdk/client-s3');
      if (!isRecord(loaded) || typeof loaded['S3Client'] !== 'function') {
        throw new Error('module shape mismatch');
      }
      this.sdk = loaded as unknown as S3ModuleLike;
      return this.sdk;
    } catch (error) {
      this.logger.error(`@aws-sdk/client-s3 indisponível: ${String(error)}`);
      throw ApiError.storageUnavailable({
        backend: 's3',
        reason: 'aws_sdk_not_installed',
        hint: 'npm i @aws-sdk/client-s3',
      });
    }
  }
}
