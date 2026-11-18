import { createHash } from 'node:crypto';
import { createReadStream, createWriteStream, promises as fs } from 'node:fs';
import * as path from 'node:path';
import { Readable, Transform } from 'node:stream';
import { pipeline } from 'node:stream/promises';

import { Injectable, Logger, type OnModuleInit } from '@nestjs/common';

import { ApiError } from '../common/errors/api-error';
import { ERROR_CODES } from '../common/errors/error-codes';
import { AppConfigService } from '../config/app-config.service';

import type { StorageService, StoredObject } from './storage.service';

/**
 * Filesystem storage. Every path is resolved and verified to stay inside the
 * configured root, so a malicious key (`../../etc/passwd`) is rejected.
 */
@Injectable()
export class LocalStorageService implements StorageService, OnModuleInit {
  readonly backend = 'local' as const;
  private readonly logger = new Logger(LocalStorageService.name);
  private readonly root: string;

  constructor(config: AppConfigService) {
    this.root = path.resolve(process.cwd(), config.storageLocalRoot);
  }

  async onModuleInit(): Promise<void> {
    await fs.mkdir(this.root, { recursive: true });
    this.logger.log(`Armazenamento local em ${this.root}`);
  }

  async put(key: string, data: Buffer): Promise<StoredObject> {
    const target = this.resolve(key);
    await fs.mkdir(path.dirname(target), { recursive: true });
    await fs.writeFile(target, data);
    return { key, size: data.length };
  }

  async get(key: string): Promise<Buffer> {
    const target = this.resolve(key);
    try {
      return await fs.readFile(target);
    } catch {
      throw ApiError.notFound({ reason: 'storage_object_not_found', key });
    }
  }

  async delete(key: string): Promise<void> {
    const target = this.resolve(key);
    try {
      await fs.unlink(target);
    } catch (error) {
      const code = (error as { code?: string }).code;
      if (code !== 'ENOENT') throw error;
    }
  }

  async exists(key: string): Promise<boolean> {
    try {
      await fs.access(this.resolve(key));
      return true;
    } catch {
      return false;
    }
  }

  async putStream(key: string, stream: Readable, _contentType: string): Promise<StoredObject> {
    const target = this.resolve(key);
    await fs.mkdir(path.dirname(target), { recursive: true });
    const temporary = `${target}.${process.pid}.${Date.now()}.part`;
    const hash = createHash('sha256');
    let size = 0;
    const counter = new Transform({
      transform(chunk: Buffer, _encoding, callback) {
        hash.update(chunk);
        size += chunk.length;
        callback(null, chunk);
      },
    });
    try {
      await pipeline(stream, counter, createWriteStream(temporary));
      // Only a fully written part becomes visible under its final key.
      await fs.rename(temporary, target);
    } catch (error) {
      await fs.rm(temporary, { force: true }).catch(() => undefined);
      throw error;
    }
    return { key, size };
  }

  async openRead(key: string): Promise<Readable> {
    return createReadStream(this.resolve(key));
  }

  async size(key: string): Promise<number> {
    try {
      const stats = await fs.stat(this.resolve(key));
      return stats.size;
    } catch {
      return -1;
    }
  }

  async move(sourceKey: string, targetKey: string): Promise<void> {
    const source = this.resolve(sourceKey);
    const target = this.resolve(targetKey);
    await fs.mkdir(path.dirname(target), { recursive: true });
    try {
      await fs.rename(source, target);
    } catch {
      // Different volumes: copy then remove.
      await fs.copyFile(source, target);
      await fs.rm(source, { force: true });
    }
  }

  async compose(partKeys: string[], targetKey: string, _contentType: string): Promise<StoredObject> {
    const target = this.resolve(targetKey);
    await fs.mkdir(path.dirname(target), { recursive: true });
    const temporary = `${target}.${process.pid}.${Date.now()}.compose`;
    const handle = await fs.open(temporary, 'w');
    let size = 0;
    try {
      for (const key of partKeys) {
        const part = await fs.open(this.resolve(key), 'r');
        try {
          let position = 0;
          const stats = await part.stat();
          while (position < stats.size) {
            const length = Math.min(4 * 1024 * 1024, stats.size - position);
            const buffer = Buffer.allocUnsafe(length);
            const { bytesRead } = await part.read(buffer, 0, length, position);
            if (bytesRead === 0) break;
            await handle.write(buffer, 0, bytesRead);
            position += bytesRead;
            size += bytesRead;
          }
        } finally {
          await part.close();
        }
      }
    } catch (error) {
      await handle.close();
      await fs.rm(temporary, { force: true }).catch(() => undefined);
      throw error;
    }
    await handle.close();
    await fs.rename(temporary, target);
    return { key: targetKey, size };
  }

  /** Path-traversal safe resolution. */
  private resolve(key: string): string {
    const normalized = key.replace(/\\/g, '/').replace(/^\/+/, '');
    if (normalized.includes('\0')) {
      throw ApiError.badRequest(ERROR_CODES.INVALID_FILE, { reason: 'invalid_storage_key' });
    }
    const target = path.resolve(this.root, normalized);
    const relative = path.relative(this.root, target);
    if (relative.startsWith('..') || path.isAbsolute(relative) || relative.length === 0) {
      throw ApiError.badRequest(ERROR_CODES.INVALID_FILE, { reason: 'path_traversal' });
    }
    return target;
  }
}
