import { constants as fsConstants, promises as fs } from 'node:fs';
import * as path from 'node:path';

import { Injectable, Logger } from '@nestjs/common';

import { AppConfigService } from '../../config/app-config.service';
import { PrismaService } from '../../database/prisma.service';
import { ConverterClient } from '../conversions/converter.client';

export type ServiceState = 'up' | 'down' | 'degraded';

export interface HealthReport {
  status: 'ok' | 'degraded';
  services: {
    db: ServiceState;
    queue: ServiceState;
    storage: ServiceState;
    converter: ServiceState;
  };
  version: string;
  uptime_seconds: number;
}

@Injectable()
export class HealthService {
  private readonly logger = new Logger(HealthService.name);

  constructor(
    private readonly prisma: PrismaService,
    private readonly converter: ConverterClient,
    private readonly config: AppConfigService,
  ) {}

  async check(): Promise<HealthReport> {
    const [db, converter, storage, queue] = await Promise.all([
      this.checkDb(),
      this.checkConverter(),
      this.checkStorage(),
      this.checkQueue(),
    ]);

    const services = { db, queue, storage, converter };
    const status = Object.values(services).every((state) => state === 'up') ? 'ok' : 'degraded';

    return {
      status,
      services,
      version: this.config.engineVersion,
      uptime_seconds: Math.round(process.uptime()),
    };
  }

  private async checkDb(): Promise<ServiceState> {
    const ok = await this.prisma.ping();
    return ok ? 'up' : 'down';
  }

  private async checkConverter(): Promise<ServiceState> {
    const health = await this.converter.safeHealth();
    if (!health) return 'down';
    return health.status === 'ok' || health.status === 'healthy' ? 'up' : 'degraded';
  }

  private async checkStorage(): Promise<ServiceState> {
    if (this.config.storageBackend === 's3') {
      // Reachability is validated lazily on use; report the configured backend.
      return (await this.converter.safeHealth()) ? 'up' : 'degraded';
    }

    try {
      const root = path.resolve(process.cwd(), this.config.storageLocalRoot);
      await fs.mkdir(root, { recursive: true });
      await fs.access(root, fsConstants.W_OK);
      return 'up';
    } catch (error) {
      this.logger.error(`Armazenamento local inacessível: ${String(error)}`);
      return 'down';
    }
  }

  private async checkQueue(): Promise<ServiceState> {
    try {
      const pending = await this.prisma.outboxEvent.count({ where: { status: 'pending' } });
      return pending > 0 ? 'degraded' : 'up';
    } catch {
      return 'down';
    }
  }
}
