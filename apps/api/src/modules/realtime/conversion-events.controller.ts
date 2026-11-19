import {
  Controller,
  Get,
  Headers,
  Param,
  Query,
  Req,
  Res,
  UseGuards,
} from '@nestjs/common';
import type { Request, Response } from 'express';
import type { Subscription } from 'rxjs';

import { CurrentUser } from '../../common/decorators/current-user.decorator';
import { ApiError } from '../../common/errors/api-error';
import { ERROR_CODES } from '../../common/errors/error-codes';
import type { AuthenticatedUser } from '../../common/request-context';
import { isTerminal } from '../../common/status';
import { PrismaService } from '../../database/prisma.service';
import { OptionalJwtGuard } from '../../auth/guards/optional-jwt.guard';

import { ConversionEventBus } from './conversion-event-bus.service';
import { ConversionEventsService } from './conversion-events.service';
import { HEARTBEAT_INTERVAL_MS, type SseEvent } from './realtime.types';
import { SequencedDelivery } from './sequenced-delivery';

const REPLAY_PAGE_SIZE = 500;
const RUN_ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;

function parseCursor(value: string | string[] | undefined): number | null {
  const raw = Array.isArray(value) ? value[0] : value;
  if (!raw) return null;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

/** Optional `run_id` filter. Absent or empty means "the conversion as a whole". */
function parseRunId(value: string | string[] | undefined): string | null {
  const raw = Array.isArray(value) ? value[0] : value;
  if (!raw) return null;
  const trimmed = raw.trim();
  if (!RUN_ID_PATTERN.test(trimmed)) {
    throw ApiError.badRequest(ERROR_CODES.VALIDATION_ERROR, { reason: 'invalid_run_id' });
  }
  return trimmed;
}

/**
 * `GET /conversions/:id/events` — text/event-stream with resume support.
 *
 * EventSource cannot send headers, so the token can also arrive as
 * `?access_token=`, and the cursor as `?last_event_id=` or `Last-Event-ID`.
 *
 * `?run_id=` follows one run only: its events are replayed and streamed, and
 * the stream ends on that run's terminal event. Without it, the stream follows
 * the whole conversion and ends only on a terminal event of the CURRENT run.
 * A retry reuses the conversion id, so an older run's failure must not close it.
 */
@Controller('conversions')
@UseGuards(OptionalJwtGuard)
export class ConversionEventsController {
  constructor(
    private readonly prisma: PrismaService,
    private readonly events: ConversionEventsService,
    private readonly bus: ConversionEventBus,
  ) {}

  @Get(':id/events')
  async stream(
    @Param('id') id: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
    @Req() request: Request,
    @Res() response: Response,
    @Headers('last-event-id') lastEventIdHeader?: string,
    @Query('last_event_id') lastEventIdQuery?: string,
    @Query('run_id') runIdQuery?: string,
  ): Promise<void> {
    if (!user) throw ApiError.unauthenticated();

    const conversion = await this.prisma.conversion.findUnique({
      where: { id },
      include: { runs: { orderBy: { attempt: 'desc' }, take: 1 } },
    });
    if (!conversion || conversion.deletedAt) throw ApiError.notFound({ resource: 'conversion' });
    if (conversion.ownerId !== user.id) throw ApiError.forbidden();

    const runFilter = parseRunId(runIdQuery);
    if (runFilter) {
      const run = await this.prisma.conversionRun.findFirst({
        where: { id: runFilter, conversionId: id },
        select: { id: true },
      });
      if (!run) throw ApiError.notFound({ resource: 'conversion_run' });
    }

    const cursor = parseCursor(lastEventIdHeader) ?? parseCursor(lastEventIdQuery) ?? 0;
    // Snapshot of the run the conversion is on now. Refined per terminal event below.
    let currentRunId: string | null = conversion.activeRunId ?? conversion.runs[0]?.id ?? null;

    response.status(200);
    response.setHeader('Content-Type', 'text/event-stream; charset=utf-8');
    response.setHeader('Cache-Control', 'no-cache, no-transform');
    response.setHeader('Connection', 'keep-alive');
    response.setHeader('X-Accel-Buffering', 'no');
    response.flushHeaders();
    response.write(': connected\n\n');

    let closed = false;
    let subscription: Subscription | null = null;
    let heartbeat: NodeJS.Timeout | null = null;

    const cleanup = (): void => {
      if (closed) return;
      closed = true;
      if (heartbeat) clearInterval(heartbeat);
      heartbeat = null;
      subscription?.unsubscribe();
      subscription = null;
      this.bus.release(id);
    };

    const finish = (): void => {
      cleanup();
      if (!response.writableEnded) response.end();
    };

    const write = (chunk: string): void => {
      if (closed || response.writableEnded) return;
      try {
        response.write(chunk);
      } catch {
        cleanup();
      }
    };

    /** The run that a terminal event belongs to decides whether the stream closes. */
    const closesStream = (event: SseEvent): void => {
      if (runFilter) {
        if (event.run_id === runFilter) finish();
        return;
      }
      if (!event.run_id || event.run_id === currentRunId) {
        finish();
        return;
      }
      // An older run's terminal event, unless the conversion has moved on to
      // that run in the meantime (a retry raced with this stream).
      void this.activeRunOf(id)
        .then((active) => {
          if (closed || active !== event.run_id) return;
          currentRunId = active;
          finish();
        })
        .catch(() => undefined);
    };

    const deliver = (event: SseEvent): void => {
      if (closed) return;
      if (runFilter && event.run_id !== runFilter) return;
      write(`id: ${event.sequence}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
      if (isTerminal(event.status)) closesStream(event);
    };

    request.on('close', finish);

    heartbeat = setInterval(() => {
      write(`: heartbeat ${Date.now()}\n\n`);
    }, HEARTBEAT_INTERVAL_MS);
    heartbeat.unref?.();

    // 1) Attach to the live bus first; live events are held until the replay ends.
    const delivery = new SequencedDelivery<SseEvent>(cursor, deliver);
    subscription = this.bus.stream(id).subscribe({
      next: (event) => delivery.receive(event),
      error: () => finish(),
    });

    // 2) Replay the durable log after the cursor, page by page.
    let afterSequence = cursor;
    for (;;) {
      const page = await this.events.listAfter(id, afterSequence, REPLAY_PAGE_SIZE);
      for (const event of page) {
        afterSequence = event.sequence;
        delivery.replay(event);
        if (closed) return;
      }
      if (page.length < REPLAY_PAGE_SIZE) break;
    }

    // 3) Flush what went live during the replay, de-duplicated by sequence.
    delivery.flush();
  }

  private async activeRunOf(conversionId: string): Promise<string | null> {
    const row = await this.prisma.conversion.findUnique({
      where: { id: conversionId },
      include: { runs: { orderBy: { attempt: 'desc' }, take: 1 } },
    });
    if (!row) return null;
    return row.activeRunId ?? row.runs[0]?.id ?? null;
  }
}
