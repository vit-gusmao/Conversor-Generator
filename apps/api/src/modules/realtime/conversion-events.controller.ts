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
import type { AuthenticatedUser } from '../../common/request-context';
import { isTerminal } from '../../common/status';
import { PrismaService } from '../../database/prisma.service';
import { OptionalJwtGuard } from '../../auth/guards/optional-jwt.guard';

import { ConversionEventBus } from './conversion-event-bus.service';
import { ConversionEventsService } from './conversion-events.service';
import { HEARTBEAT_INTERVAL_MS, type SseEvent } from './realtime.types';

function parseCursor(value: string | string[] | undefined): number | null {
  const raw = Array.isArray(value) ? value[0] : value;
  if (!raw) return null;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

/**
 * `GET /conversions/:id/events` — text/event-stream with resume support.
 *
 * EventSource cannot send headers, so the token can also arrive as
 * `?access_token=`, and the cursor as `?last_event_id=` or `Last-Event-ID`.
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
  ): Promise<void> {
    if (!user) throw ApiError.unauthenticated();

    const conversion = await this.prisma.conversion.findUnique({
      where: { id },
      select: { id: true, ownerId: true, deletedAt: true, status: true },
    });
    if (!conversion || conversion.deletedAt) throw ApiError.notFound({ resource: 'conversion' });
    if (conversion.ownerId !== user.id) throw ApiError.forbidden();

    const cursor = parseCursor(lastEventIdHeader) ?? parseCursor(lastEventIdQuery) ?? 0;

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

    const write = (chunk: string): void => {
      if (closed || response.writableEnded) return;
      try {
        response.write(chunk);
      } catch {
        cleanup();
      }
    };

    const send = (event: SseEvent): void => {
      write(`id: ${event.sequence}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
      if (isTerminal(event.status)) {
        cleanup();
        if (!response.writableEnded) response.end();
      }
    };

    request.on('close', () => {
      cleanup();
      if (!response.writableEnded) response.end();
    });

    // 1) Replay the durable log after the cursor.
    const replay = await this.events.listAfter(id, cursor, 500);
    for (const event of replay) {
      send(event);
      if (closed) return;
    }

    // 2) Attach to the live stream.
    subscription = this.bus.stream(id).subscribe({
      next: (event) => send(event),
      error: () => {
        cleanup();
        if (!response.writableEnded) response.end();
      },
    });

    heartbeat = setInterval(() => {
      write(`: heartbeat ${Date.now()}\n\n`);
    }, HEARTBEAT_INTERVAL_MS);
    heartbeat.unref?.();
  }
}
