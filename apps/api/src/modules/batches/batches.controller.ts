import {
  Body,
  Controller,
  Get,
  Headers,
  HttpCode,
  HttpStatus,
  Param,
  Post,
  Query,
  Res,
  UseGuards,
} from '@nestjs/common';
import type { BatchEvent, BatchItem, BatchSummary, CursorPage } from '@pdf2x/contracts';
import type { Response } from 'express';
import { Subscription } from 'rxjs';

import { OptionalJwtGuard } from '../../auth/guards/optional-jwt.guard';
import { CurrentUser } from '../../common/decorators/current-user.decorator';
import { ApiError } from '../../common/errors/api-error';
import type { AuthenticatedUser } from '../../common/request-context';
import { PrismaService } from '../../database/prisma.service';

import { BatchEventsService } from './batch-events.service';
import {
  CreateBatchDto,
  ListBatchItemsQueryDto,
  RegisterBatchItemDto,
  RetryBatchItemDto,
} from './dto/batch.dto';
import { BatchesService } from './batches.service';

const HEARTBEAT_INTERVAL_MS = 15_000;

@Controller('batches')
@UseGuards(OptionalJwtGuard)
export class BatchesController {
  constructor(
    private readonly batches: BatchesService,
    private readonly events: BatchEventsService,
    private readonly prisma: PrismaService,
  ) {}

  @Post()
  @HttpCode(HttpStatus.CREATED)
  create(
    @Body() body: CreateBatchDto,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<BatchSummary> {
    if (!user) throw ApiError.unauthenticated();
    return this.batches.create(user, body.client_batch_key);
  }

  @Get(':id')
  get(
    @Param('id') id: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<BatchSummary> {
    if (!user) throw ApiError.unauthenticated();
    return this.batches.get(user, id);
  }

  @Post(':id/items')
  @HttpCode(HttpStatus.CREATED)
  registerItem(
    @Param('id') id: string,
    @Body() body: RegisterBatchItemDto,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ) {
    if (!user) throw ApiError.unauthenticated();
    return this.batches.registerItem(user, id, body);
  }

  @Get(':id/items')
  listItems(
    @Param('id') id: string,
    @Query() query: ListBatchItemsQueryDto,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<CursorPage<BatchItem>> {
    if (!user) throw ApiError.unauthenticated();
    return this.batches.listItems(user, id, query);
  }

  @Post(':id/seal')
  @HttpCode(HttpStatus.OK)
  seal(
    @Param('id') id: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ) {
    if (!user) throw ApiError.unauthenticated();
    return this.batches.seal(user, id);
  }

  @Post(':id/cancel')
  @HttpCode(HttpStatus.OK)
  cancel(
    @Param('id') id: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<BatchSummary> {
    if (!user) throw ApiError.unauthenticated();
    return this.batches.cancel(user, id);
  }

  @Post(':id/items/:itemId/cancel')
  @HttpCode(HttpStatus.OK)
  cancelItem(
    @Param('id') id: string,
    @Param('itemId') itemId: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<BatchItem> {
    if (!user) throw ApiError.unauthenticated();
    return this.batches.cancelItem(user, id, itemId);
  }

  @Post(':id/items/:itemId/retry')
  @HttpCode(HttpStatus.ACCEPTED)
  retryItem(
    @Param('id') id: string,
    @Param('itemId') itemId: string,
    @Body() body: RetryBatchItemDto,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ) {
    if (!user) throw ApiError.unauthenticated();
    return this.batches.retryItem(user, id, itemId, body.scope ?? 'conversion');
  }

  /**
   * Aggregated batch SSE. The cursor is persisted per batch, so a reconnect
   * with `Last-Event-ID` replays exactly what was missed.
   */
  @Get(':id/events')
  async stream(
    @Param('id') id: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
    @Res() response: Response,
    @Headers('last-event-id') lastEventId?: string,
    @Query('last_event_id') lastEventIdQuery?: string,
    @Query('access_token') accessToken?: string,
  ): Promise<void> {
    if (!user) throw ApiError.unauthenticated();

    const batch = await this.prisma.batch.findUnique({ where: { id } });
    if (!batch) throw ApiError.notFound({ resource: 'batch' });
    if (batch.ownerId !== user.id) throw ApiError.forbidden({ resource: 'batch' });

    let cursor = 0;
    for (const candidate of [lastEventId, lastEventIdQuery]) {
      const parsed = Number.parseInt(String(candidate ?? ''), 10);
      if (Number.isFinite(parsed) && parsed > cursor) cursor = parsed;
    }
    void accessToken; // accepted for EventSource clients; identity is already resolved

    response.setHeader('Content-Type', 'text/event-stream; charset=utf-8');
    response.setHeader('Cache-Control', 'no-cache, no-transform');
    response.setHeader('Connection', 'keep-alive');
    response.setHeader('X-Accel-Buffering', 'no');
    response.flushHeaders?.();
    response.write(': connected\n\n');

    const write = (event: BatchEvent): void => {
      response.write(`id: ${event.sequence}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
    };

    let closed = false;
    let subscription: Subscription | null = null;
    const cleanup = (): void => {
      if (closed) return;
      closed = true;
      clearInterval(heartbeat);
      subscription?.unsubscribe();
      response.end();
    };
    const heartbeat = setInterval(() => {
      if (!closed) response.write(': heartbeat\n\n');
    }, HEARTBEAT_INTERVAL_MS);
    heartbeat.unref?.();

    response.on('close', cleanup);

    const replay = await this.events.listAfter(id, cursor, 500);
    for (const event of replay) {
      cursor = Math.max(cursor, event.sequence);
      write(event);
    }

    const TERMINAL = new Set(['completed', 'completed_with_errors', 'cancelled']);
    const fresh = await this.prisma.batch.findUnique({ where: { id } });
    if (replay.length === 0 && fresh && TERMINAL.has(fresh.status)) {
      cleanup();
      return;
    }

    subscription = this.events.stream(id).subscribe({
      next: (event) => {
        if (event.sequence <= cursor) return;
        cursor = event.sequence;
        write(event);
        if (TERMINAL.has(event.status) && (event.type === 'batch' || event.type === 'completed')) {
          // Do not close on the first terminal-looking item event; only the
          // final batch roll-up ends the stream.
          void this.prisma.batch
            .findUnique({ where: { id }, select: { status: true } })
            .then((row) => {
              if (row && TERMINAL.has(row.status)) cleanup();
            })
            .catch(() => undefined);
        }
      },
      error: () => cleanup(),
      complete: () => cleanup(),
    });
  }
}
