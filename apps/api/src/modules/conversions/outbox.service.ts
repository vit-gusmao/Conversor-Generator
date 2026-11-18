import { Injectable, Logger, Optional, type OnModuleDestroy, type OnModuleInit } from '@nestjs/common';
import type { ConversionOptionsV2 } from '@pdf2x/contracts';
import type { Prisma } from '@prisma/client';

import { parseJson } from '../../database/json.util';
import { PrismaService } from '../../database/prisma.service';
import { ConversionEventsService } from '../realtime/conversion-events.service';
import { ConverterClient } from './converter.client';

export const OUTBOX_ENQUEUE = 'conversion.enqueue';
/** Asks the engine to confirm or refuse a cancellation atomically. */
export const OUTBOX_CANCEL = 'conversion.cancel';

export interface EnqueueOutboxPayload {
  conversionId: string;
  runId: string;
  storageKey: string;
  filename: string;
  /**
   * Canonical options. Typed as the full contract so `text`, `csv` and
   * `target_formats` are no longer narrowed away before dispatch.
   */
  options?: {
    conversionOptions?: ConversionOptionsV2;
    ocr?: boolean;
    forceOcr?: boolean;
    targetFormats?: string[];
  };
}

export interface CancelOutboxPayload {
  conversionId: string;
  runId: string;
}

interface OutboxRow {
  id: string;
  type: string;
  payloadJson: string;
  status: string;
  attempts: number;
  lockedUntil: Date | null;
}

const MAX_ATTEMPTS = 8;
const DRAIN_INTERVAL_MS = 5000;
/** How long one dispatcher owns a row. Long enough for a 30 s engine call. */
const LEASE_MS = 60_000;

/**
 * Transactional outbox for engine jobs.
 *
 * The job row is committed before the HTTP call, so a crash between "task
 * created" and "engine enqueued" is recovered by the drainer instead of
 * leaving a conversion stuck in `queued` forever.
 *
 * Dispatch is claimed with a compare-and-set lease: two API instances (or two
 * drainer ticks) can look at the same row without both calling the engine.
 * The engine itself is idempotent per `run_id`, so a lease that expires after a
 * successful call only causes a harmless replay.
 */
@Injectable()
export class OutboxService implements OnModuleInit, OnModuleDestroy {
  private readonly logger = new Logger(OutboxService.name);
  private readonly workerId = `outbox-${process.pid}-${Math.random().toString(36).slice(2, 8)}`;
  private timer: NodeJS.Timeout | null = null;
  private draining = false;

  constructor(
    private readonly prisma: PrismaService,
    private readonly converter: ConverterClient,
    @Optional() private readonly events?: ConversionEventsService,
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

    const claimed = await this.tryClaim(row);
    if (!claimed) return;

    await this.runClaimed(row);
  }

  /**
   * Atomically moves a row into `processing` for this worker.
   * Returns false when another worker got it first.
   */
  private async tryClaim(row: OutboxRow): Promise<boolean> {
    const now = new Date();
    const result = await this.prisma.outboxEvent.updateMany({
      where: {
        id: row.id,
        status: row.status,
        ...(row.status === 'processing'
          ? { lockedUntil: { lt: now } }
          : { availableAt: { lte: now } }),
      },
      data: {
        status: 'processing',
        lockedBy: this.workerId,
        lockedUntil: new Date(now.getTime() + LEASE_MS),
      },
    });
    return result.count === 1;
  }

  private async runClaimed(row: OutboxRow): Promise<void> {
    try {
      await this.execute(row.type, parseJson<Record<string, unknown>>(row.payloadJson) ?? {});
      await this.prisma.outboxEvent.update({
        where: { id: row.id },
        data: {
          status: 'dispatched',
          dispatchedAt: new Date(),
          attempts: { increment: 1 },
          lastError: null,
          lockedBy: null,
          lockedUntil: null,
        },
      });
    } catch (error) {
      const attempts = row.attempts + 1;
      const exhausted = attempts >= MAX_ATTEMPTS;
      const delayMs = Math.min(60000, 1000 * 2 ** Math.min(attempts, 6));
      const message =
        error instanceof Error ? error.message.slice(0, 500) : String(error).slice(0, 500);

      await this.prisma.outboxEvent.update({
        where: { id: row.id },
        data: {
          attempts,
          status: exhausted ? 'failed' : 'pending',
          lastError: message,
          availableAt: new Date(Date.now() + delayMs),
          lockedBy: null,
          lockedUntil: null,
        },
      });

      if (exhausted) {
        // A conversion must never sit in `queued` forever because delivery died.
        await this.failDelivery(row, attemptMessage(attempts, message));
      }

      this.logger.warn(`Outbox ${row.id} falhou (tentativa ${attempts}): ${String(error)}`);
    }
  }

  /** Marks the conversion failed so the user sees a recoverable error. */
  private async failDelivery(row: OutboxRow, reason: string): Promise<void> {
    const payload = parseJson<Record<string, unknown>>(row.payloadJson) ?? {};
    const conversionId = typeof payload['conversionId'] === 'string' ? payload['conversionId'] : null;
    const runId = typeof payload['runId'] === 'string' ? payload['runId'] : null;
    if (!conversionId) return;

    try {
      await this.prisma.conversion.updateMany({
        where: { id: conversionId, status: { notIn: ['completed', 'completed_with_warnings', 'failed', 'cancelled'] } },
        data: {
          status: 'failed',
          stage: 'failed',
          errorCode: 'QUEUE_DELIVERY_FAILED',
          errorMessage: reason,
        },
      });
      if (runId) {
        await this.prisma.conversionRun.updateMany({
          where: { id: runId },
          data: {
            status: 'failed',
            stage: 'failed',
            finishedAt: new Date(),
            errorCode: 'QUEUE_DELIVERY_FAILED',
            errorMessage: reason,
          },
        });
      }
      await this.events?.record(conversionId, {
        type: 'failed',
        status: 'failed',
        stage: 'failed',
        progress: 0,
        message: reason,
        runId,
        payload: { code: 'QUEUE_DELIVERY_FAILED', outbox_id: row.id },
      });
    } catch (error) {
      this.logger.error(
        `Não foi possível marcar a conversão ${conversionId} como falha de entrega: ${String(error)}`,
      );
    }
  }

  async drain(limit = 20): Promise<void> {
    if (this.draining) return;
    this.draining = true;
    try {
      const now = new Date();
      const candidates = (await this.prisma.outboxEvent.findMany({
        where: {
          OR: [
            { status: 'pending', availableAt: { lte: now } },
            { status: 'processing', lockedUntil: { lt: now } },
          ],
        },
        orderBy: { createdAt: 'asc' },
        take: limit,
      })) as OutboxRow[];

      for (const row of candidates) {
        if (await this.tryClaim(row)) await this.runClaimed(row);
      }
    } catch (error) {
      this.logger.warn(`Falha ao drenar outbox: ${String(error)}`);
    } finally {
      this.draining = false;
    }
  }

  /** Re-queues a `failed` row. Used by the admin/resend path. */
  async requeue(id: string): Promise<boolean> {
    const result = await this.prisma.outboxEvent.updateMany({
      where: { id, status: 'failed' },
      data: {
        status: 'pending',
        attempts: 0,
        availableAt: new Date(),
        lockedBy: null,
        lockedUntil: null,
        lastError: null,
      },
    });
    return result.count === 1;
  }

  private async execute(type: string, payload: Record<string, unknown>): Promise<void> {
    if (type === OUTBOX_CANCEL) {
      const data = payload as unknown as CancelOutboxPayload;
      if (!data.runId) throw new Error('Payload de cancelamento incompleto');
      // The engine decides atomically between "cancelled" and "already
      // published". A 404 means the run never reached it, which is a valid
      // cancellation outcome and must not be retried forever.
      await this.converter.cancel(data.runId).catch((error: unknown) => {
        if (isNotFound(error)) return;
        throw error;
      });
      return;
    }

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

function attemptMessage(attempts: number, detail: string): string {
  return `Não foi possível entregar a tarefa ao motor após ${attempts} tentativas. ${detail}`.slice(
    0,
    500,
  );
}

function isNotFound(error: unknown): boolean {
  return (
    typeof error === 'object' &&
    error !== null &&
    (error as { status?: number }).status === 404
  );
}
