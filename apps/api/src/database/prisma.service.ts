import { Injectable, Logger, type OnModuleDestroy, type OnModuleInit } from '@nestjs/common';
import { PrismaClient } from '@prisma/client';

import { AppConfigService } from '../config/app-config.service';

/**
 * Prisma client with Nest lifecycle hooks.
 *
 * The datasource URL is `AppConfigService.databaseUrl`: `API_DATABASE_URL`, or
 * `API_POSTGRES_URL` when the first is unset (see `configuration.ts`). Prisma
 * resolves relative SQLite paths against the schema directory, i.e.
 * `prisma/data/api.db`.
 */
@Injectable()
export class PrismaService extends PrismaClient implements OnModuleInit, OnModuleDestroy {
  private readonly logger = new Logger(PrismaService.name);

  constructor(config: AppConfigService) {
    super({ datasources: { db: { url: config.databaseUrl } } });
  }

  async onModuleInit(): Promise<void> {
    await this.$connect();
    this.logger.log('Banco de dados conectado');
  }

  async onModuleDestroy(): Promise<void> {
    await this.$disconnect();
  }

  /** Lightweight liveness probe used by /health. */
  async ping(): Promise<boolean> {
    try {
      await this.$queryRaw`SELECT 1`;
      return true;
    } catch (error) {
      this.logger.error('Falha no ping do banco', error instanceof Error ? error.stack : String(error));
      return false;
    }
  }
}
