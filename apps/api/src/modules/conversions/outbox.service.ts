import {
  Injectable,
  Logger,
  type OnModuleDestroy,
  type OnModuleInit,
} from '@nestjs/common';

import type { Prisma } from '@prisma/client';

import { parseJson } from '../../database/json.util';
import { PrismaService } from '../../database/prisma.service';
import { ConverterClient } from './converter.client';

export const OUTBOX_ENQUEUE = 'conversion.enqueue';

export interface EnqueueOutboxPayload {
  conversionId: string;
  runId: string;
  storageKey: string;
  filename: string;
  options?: { maxPages?: number; ocr?: boolean; forceOcr?: boolean };
}

interface OutboxRow {
  id: string;
  type: string;
  payloadJson: string;
  status: string;
  attempts: number;
}

const MAX_ATTEMPTS = 8;
const DRAIN_INTERVAL_MS = 5000;

/**
 * Transactional outbox for engine jobs.
 *
 * The job row is committed before the HTTP call, so a crash between "task
 * created" and "engine enqueued" is recovered by the drainer instead of
 * leaving a conversion stuck in `queued` forever.
 */
@Injectable()
export class OutboxService implements OnModuleInit, OnModuleDestroy {
  private readonly logger = new Logger(OutboxService.name);
  private timer: NodeJS.Timeout | null = null;
  private draining = false;

  constructor(
    private readonly prisma: PrismaService,
    private readonly converter: ConverterClient,
  ) {}

  onModuleInit(): void {
    this.timer = setInterval(() => {
      void this.drain();
    }, DRAIN_INTERVAL_MS);
    this.timer.unref();
  }

  onModuleDestroy(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  async record(
    type: string,
    payload: Record<string, unknown>,
    tx?: Prisma.TransactionClient,
  ): Promise<{ id: string }> {
    const client = tx ?? (this.prisma as unknown as Prisma.TransactionClient);
    const row = await client.outboxEvent.create({
      data: {
        type,
        payloadJson: JSON.stringify(payload),
        status: 'pending',
        availableAt: new Date(),
      },
    });
    return { id: row.id };
  }

  /** Best-effort immediate dispatch; failures stay in the outbox for retry. */
  async dispatch(id: string): Promise<void> {
    const row = (await this.prisma.outboxEvent.findUnique({ where: { id } })) as OutboxRow | null;
    if (!row || row.status === 'dispatched' || row.status === 'failed') return;

    try {
      await this.execute(row.type, parseJson<Record<string, unknown>>(row.payloadJson) ?? {});
      await this.prisma.outboxEvent.update({
        where: { id },
        data: {
          status: 'dispatched',
          dispatchedAt: new Date(),
          attempts: { increment: 1 },
          lastError: null,
        },
      });
    } catch (error) {
      const attempts = row.attempts + 1;
      const delayMs = Math.min(60000, 1000 * 2 ** Math.min(attempts, 6));
      await this.prisma.outboxEvent.update({
        where: { id },
        data: {
          attempts,
          status: attempts >= MAX_ATTEMPTS ? 'failed' : 'pending',
          lastError: error instanceof Error ? error.message.slice(0, 500) : String(error).slice(0, 500),
          availableAt: new Date(Date.now() + delayMs),
        },
      });
      this.logger.warn(`Outbox ${id} falhou (tentativa ${attempts}): ${String(error)}`);
    }
  }

  async drain(limit = 20): Promise<void> {
    if (this.draining) return;
    this.draining = true;
    try {
      const rows = (await this.prisma.outboxEvent.findMany({
        where: { status: 'pending', availableAt: { lte: new Date() } },
        orderBy: { createdAt: 'asc' },
        take: limit,
      })) as OutboxRow[];

      for (const row of rows) {
        await this.dispatch(row.id);
      }
    } catch (error) {
      this.logger.warn(`Falha ao drenar outbox: ${String(error)}`);
    } finally {
      this.draining = false;
    }
  }

  private async execute(type: string, payload: Record<string, unknown>): Promise<void> {
    if (type !== OUTBOX_ENQUEUE) {
      throw new Error(`Tipo de outbox desconhecido: ${type}`);
    }

    const data = payload as unknown as EnqueueOutboxPayload;
    if (!data.runId || !data.conversionId || !data.storageKey) {
      throw new Error('Payload de enfileiramento incompleto');
    }

    await this.converter.enqueue({
      runId: data.runId,
      conversionId: data.conversionId,
      storageKey: data.storageKey,
      filename: data.filename ?? 'documento.pdf',
      options: data.options,
    });
  }
}
