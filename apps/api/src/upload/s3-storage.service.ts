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
