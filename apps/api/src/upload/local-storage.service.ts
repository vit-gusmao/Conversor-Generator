import { promises as fs } from 'node:fs';
import * as path from 'node:path';

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
