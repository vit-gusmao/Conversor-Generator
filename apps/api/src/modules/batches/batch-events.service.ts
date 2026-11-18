import { Injectable, Logger, type OnModuleDestroy } from '@nestjs/common';
import type { BatchEvent } from '@pdf2x/contracts';
import { Subject, type Observable } from 'rxjs';

import { PrismaService } from '../../database/prisma.service';
import { parseJson } from '../../database/json.util';

interface BatchEventRow {
  id: string;
  batchId: string;
  sequence: number;
  type: string;
  itemId: string | null;
  status: string;
  itemStatus: string | null;
  message: string;
  payloadJson: string | null;
  createdAt: Date;
}

const MAX_MESSAGE_LENGTH = 500;
const MAX_REPLAY_LIMIT = 1000;

/**
 * Persisted, sequenced batch events plus an in-process fan-out.
 *
 * The database row is the source of truth: a client that reconnects with
 * `Last-Event-ID` replays what it missed, and losing an in-memory notification
 * only costs a poll, never an event.
 */
@Injectable()
export class BatchEventsService implements OnModuleDestroy {
  private readonly logger = new Logger(BatchEventsService.name);
  private readonly streams = new Map<string, Subject<BatchEvent>>();

  constructor(private readonly prisma: PrismaService) {}

  onModuleDestroy(): void {
    for (const subject of this.streams.values()) subject.complete();
    this.streams.clear();
  }

  stream(batchId: string): Observable<BatchEvent> {
    const existing = this.streams.get(batchId);
    if (existing) return existing.asObservable();
    const subject = new Subject<BatchEvent>();
    this.streams.set(batchId, subject);
    return subject.asObservable();
  }

  async record(
    batchId: string,
    input: {
      type?: BatchEvent['type'];
      itemId?: string | null;
      status: string;
      itemStatus?: string | null;
      message?: string;
      payload?: Record<string, unknown>;
    },
  ): Promise<BatchEvent | null> {
    const sequence = await this.nextSequence(batchId);
    try {
      const row = (await this.prisma.batchEvent.create({
        data: {
          batchId,
          sequence,
          type: input.type ?? 'batch',
          itemId: input.itemId ?? null,
          status: input.status,
          itemStatus: input.itemStatus ?? null,
          message: (input.message ?? '').slice(0, MAX_MESSAGE_LENGTH),
          payloadJson: input.payload ? JSON.stringify(input.payload) : null,
        },
      })) as BatchEventRow;
      const event = this.toEvent(row);
      this.streams.get(batchId)?.next(event);
      return event;
    } catch (error) {
      // A concurrent writer took the sequence; retry once with a fresh one.
      this.logger.debug(`Corrida de sequência no lote ${batchId}: ${String(error)}`);
      try {
        const retrySequence = await this.nextSequence(batchId);
        const row = (await this.prisma.batchEvent.create({
          data: {
            batchId,
            sequence: retrySequence,
            type: input.type ?? 'batch',
            itemId: input.itemId ?? null,
            status: input.status,
            itemStatus: input.itemStatus ?? null,
            message: (input.message ?? '').slice(0, MAX_MESSAGE_LENGTH),
            payloadJson: input.payload ? JSON.stringify(input.payload) : null,
          },
        })) as BatchEventRow;
        const event = this.toEvent(row);
        this.streams.get(batchId)?.next(event);
        return event;
      } catch (retryError) {
        this.logger.warn(`Não foi possível registrar evento do lote ${batchId}: ${String(retryError)}`);
        return null;
      }
    }
  }

  async listAfter(batchId: string, cursor: number, limit = 200): Promise<BatchEvent[]> {
    const rows = (await this.prisma.batchEvent.findMany({
      where: { batchId, sequence: { gt: cursor } },
      orderBy: { sequence: 'asc' },
      take: Math.min(limit, MAX_REPLAY_LIMIT),
    })) as BatchEventRow[];
    return rows.map((row) => this.toEvent(row));
  }

  async maxSequence(batchId: string): Promise<number> {
    const result = await this.prisma.batchEvent.aggregate({
      where: { batchId },
      _max: { sequence: true },
    });
    return result._max.sequence ?? 0;
  }

  toEvent(row: BatchEventRow): BatchEvent {
    return {
      batch_id: row.batchId,
      sequence: row.sequence,
      type: (row.type as BatchEvent['type']) ?? 'batch',
      item_id: row.itemId,
      status: row.status as BatchEvent['status'],
      item_status: (row.itemStatus as BatchEvent['item_status']) ?? null,
      message: row.message,
      payload: parseJson<Record<string, unknown>>(row.payloadJson) ?? {},
      at: row.createdAt.toISOString(),
    };
  }

  private async nextSequence(batchId: string): Promise<number> {
    return (await this.maxSequence(batchId)) + 1;
  }
}
