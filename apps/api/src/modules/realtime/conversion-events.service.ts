import { Injectable, Logger } from '@nestjs/common';
import type { ConversionEvent } from '@pdf2x/contracts';

import { asConversionStatus, clampProgress } from '../../common/status';
import { parseJson, stringifyJson } from '../../database/json.util';
import { PrismaService } from '../../database/prisma.service';

import { ConversionEventBus } from './conversion-event-bus.service';
import type {
  ConversionEventRow,
  RecordEventInput,
  SseEvent,
  StoredEventPayload,
} from './realtime.types';

const MAX_MESSAGE_LENGTH = 500;
const MAX_REPLAY_LIMIT = 1000;

/**
 * Append-only event log used for SSE replay.
 *
 * `sequence` is per conversion and gap-free; the unique index
 * `(conversionId, sequence)` protects against concurrent writers.
 */
@Injectable()
export class ConversionEventsService {
  private readonly logger = new Logger(ConversionEventsService.name);

  constructor(
    private readonly prisma: PrismaService,
    private readonly bus: ConversionEventBus,
  ) {}

  async record(conversionId: string, input: RecordEventInput): Promise<SseEvent | null> {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const sequence = await this.nextSequence(conversionId);
      try {
        const row = await this.prisma.conversionEvent.create({
          data: {
            conversionId,
            sequence,
            type: input.type,
            status: input.status,
            stage: input.stage,
            progress: clampProgress(input.progress),
            message: input.message.slice(0, MAX_MESSAGE_LENGTH),
            payloadJson: stringifyJson({
              run_id: input.runId ?? null,
              progress_indeterminate: Boolean(input.indeterminate),
              data: input.payload ?? {},
            } satisfies StoredEventPayload),
          },
        });

        const event = this.toSse(row as ConversionEventRow);
        this.bus.publish(conversionId, event);
        return event;
      } catch (error) {
        // Unique race: recompute the sequence and try once more.
        if (attempt === 1) {
          this.logger.warn(
            `Falha ao registrar evento da conversão ${conversionId}: ${String(error)}`,
          );
          return null;
        }
      }
    }
    return null;
  }

  async listAfter(
    conversionId: string,
    cursor: number,
    limit = 200,
  ): Promise<SseEvent[]> {
    const rows = await this.prisma.conversionEvent.findMany({
      where: { conversionId, sequence: { gt: cursor } },
      orderBy: { sequence: 'asc' },
      take: Math.min(limit, MAX_REPLAY_LIMIT),
    });
    return rows.map((row) => this.toSse(row as ConversionEventRow));
  }

  async maxSequence(conversionId: string): Promise<number> {
    return (await this.nextSequence(conversionId)) - 1;
  }

  toSse(row: ConversionEventRow): SseEvent {
    const stored = parseJson<StoredEventPayload>(row.payloadJson);
    return {
      sequence: row.sequence,
      event_id: row.id,
      conversion_id: row.conversionId,
      run_id: stored?.run_id ?? '',
      type: row.type as ConversionEvent['type'],
      status: asConversionStatus(row.status) ?? 'received',
      stage: (row.stage as ConversionEvent['stage']) ?? 'receive',
      progress: row.progress,
      progress_indeterminate: stored?.progress_indeterminate ?? false,
      message: row.message,
      payload: stored?.data ?? {},
      at: row.createdAt.toISOString(),
    };
  }

  private async nextSequence(conversionId: string): Promise<number> {
    const aggregate = await this.prisma.conversionEvent.aggregate({
      where: { conversionId },
      _max: { sequence: true },
    });
    const max = aggregate._max.sequence;
    return (typeof max === 'number' ? max : 0) + 1;
  }
}
