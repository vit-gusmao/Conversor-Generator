import {
  Body,
  Controller,
  Delete,
  Get,
  Headers,
  HttpCode,
  HttpStatus,
  Param,
  Patch,
  Post,
  Put,
  Query,
  Req,
} from '@nestjs/common';
import type { Request } from 'express';

import { CurrentUser } from '../../common/decorators/current-user.decorator';
import { OptionalJwtGuard } from '../../auth/guards/optional-jwt.guard';
import { UseGuards } from '@nestjs/common';
import { ApiError } from '../../common/errors/api-error';
import type { AuthenticatedUser } from '../../common/request-context';
import { AutomationService } from './automation.service';

/**
 * Public proxy for sources/imports/schedules (T6). The browser never talks to
 * the engine directly in full mode; owner identity is attached here and
 * `owner_id` from the payload is always stripped.
 */
@Controller()
@UseGuards(OptionalJwtGuard)
export class AutomationController {
  constructor(private readonly automation: AutomationService) {}

  private requireUser(user: AuthenticatedUser | undefined): AuthenticatedUser {
    if (!user) {
      throw ApiError.unauthenticated({ reason: 'missing_session' });
    }
    return user;
  }

  @Get('source-roots')
  listRoots(@CurrentUser() user?: AuthenticatedUser): Promise<unknown> {
    const owner = this.requireUser(user);
    return this.automation.listRoots(owner.id);
  }

  @Get('sources')
  listSources(@CurrentUser() user?: AuthenticatedUser): Promise<unknown> {
    const owner = this.requireUser(user);
    return this.automation.listSources(owner.id);
  }

  @Post('sources')
  createSource(
    @CurrentUser() user?: AuthenticatedUser,
    @Body() body?: unknown,
  ): Promise<unknown> {
    const owner = this.requireUser(user);
    return this.automation.createSource(owner, body);
  }

  @Get('sources/:id')
  getSource(@Param('id') id: string, @CurrentUser() user?: AuthenticatedUser): Promise<unknown> {
    const owner = this.requireUser(user);
    return this.automation.getSource(owner.id, id);
  }

  @Put('sources/:id')
  patchSource(
    @Param('id') id: string,
    @CurrentUser() user?: AuthenticatedUser,
    @Body() body?: unknown,
  ): Promise<unknown> {
    const owner = this.requireUser(user);
    return this.automation.patchSource(owner, id, body);
  }

  /** PATCH alias of PUT: the web client may send either verb. */
  @Patch('sources/:id')
  updateSourcePatch(
    @Param('id') id: string,
    @CurrentUser() user?: AuthenticatedUser,
    @Body() body?: unknown,
  ): Promise<unknown> {
    return this.patchSource(id, user, body);
  }

  @Delete('sources/:id')
  deleteSource(@Param('id') id: string, @CurrentUser() user?: AuthenticatedUser): Promise<unknown> {
    const owner = this.requireUser(user);
    return this.automation.deleteSource(owner.id, id);
  }

  @Post('sources/:id/validate')
  validateSource(
    @Param('id') id: string,
    @CurrentUser() user?: AuthenticatedUser,
  ): Promise<unknown> {
    const owner = this.requireUser(user);
    return this.automation.validateSource(owner.id, id);
  }

  @Post('sources/:id/imports')
  @HttpCode(HttpStatus.ACCEPTED)
  startImport(
    @Param('id') id: string,
    @Req() request: Request,
    @CurrentUser() user?: AuthenticatedUser,
    @Body() body?: unknown,
  ): Promise<unknown> {
    const owner = this.requireUser(user);
    const header = request.headers['idempotency-key'];
    const key = Array.isArray(header) ? header[0] : header;
    return this.automation.startImport(owner, id, body, key);
  }

  @Get('imports/:id')
  getImport(@Param('id') id: string, @CurrentUser() user?: AuthenticatedUser): Promise<unknown> {
    const owner = this.requireUser(user);
    return this.automation.getImport(owner.id, id);
  }

  @Get('imports/:id/items')
  listImportItems(
    @Param('id') id: string,
    @CurrentUser() user?: AuthenticatedUser,
  ): Promise<unknown> {
    const owner = this.requireUser(user);
    return this.automation.listImportItems(owner.id, id);
  }

  @Post('imports/:id/cancel')
  @HttpCode(HttpStatus.ACCEPTED)
  cancelImport(@Param('id') id: string, @CurrentUser() user?: AuthenticatedUser): Promise<unknown> {
    const owner = this.requireUser(user);
    return this.automation.cancelImport(owner.id, id);
  }

  @Get('schedules')
  listSchedules(@CurrentUser() user?: AuthenticatedUser): Promise<unknown> {
    const owner = this.requireUser(user);
    return this.automation.listSchedules(owner.id);
  }

  @Post('schedules')
  createSchedule(
    @CurrentUser() user?: AuthenticatedUser,
    @Body() body?: unknown,
  ): Promise<unknown> {
    const owner = this.requireUser(user);
    return this.automation.createSchedule(owner, body);
  }

  @Get('schedules/:id')
  getSchedule(@Param('id') id: string, @CurrentUser() user?: AuthenticatedUser): Promise<unknown> {
    const owner = this.requireUser(user);
    return this.automation.getSchedule(owner.id, id);
  }

  @Put('schedules/:id')
  patchSchedule(
    @Param('id') id: string,
    @CurrentUser() user?: AuthenticatedUser,
    @Body() body?: unknown,
  ): Promise<unknown> {
    const owner = this.requireUser(user);
    return this.automation.patchSchedule(owner, id, body);
  }

  /** PATCH alias of PUT: the web client may send either verb. */
  @Patch('schedules/:id')
  updateSchedulePatch(
    @Param('id') id: string,
    @CurrentUser() user?: AuthenticatedUser,
    @Body() body?: unknown,
  ): Promise<unknown> {
    return this.patchSchedule(id, user, body);
  }

  @Delete('schedules/:id')
  deleteSchedule(
    @Param('id') id: string,
    @CurrentUser() user?: AuthenticatedUser,
  ): Promise<unknown> {
    const owner = this.requireUser(user);
    return this.automation.deleteSchedule(owner.id, id);
  }

  @Post('schedules/:id/pause')
  pause(@Param('id') id: string, @CurrentUser() user?: AuthenticatedUser): Promise<unknown> {
    const owner = this.requireUser(user);
    return this.automation.scheduleAction(owner.id, id, 'pause');
  }

  @Post('schedules/:id/resume')
  resume(@Param('id') id: string, @CurrentUser() user?: AuthenticatedUser): Promise<unknown> {
    const owner = this.requireUser(user);
    return this.automation.scheduleAction(owner.id, id, 'resume');
  }

  @Post('schedules/:id/run-now')
  @HttpCode(HttpStatus.ACCEPTED)
  runNow(@Param('id') id: string, @CurrentUser() user?: AuthenticatedUser): Promise<unknown> {
    const owner = this.requireUser(user);
    return this.automation.scheduleAction(owner.id, id, 'run-now');
  }

  @Get('schedules/:id/runs')
  runs(@Param('id') id: string, @CurrentUser() user?: AuthenticatedUser): Promise<unknown> {
    const owner = this.requireUser(user);
    return this.automation.scheduleRuns(owner.id, id);
  }

  @Get('schedule-runs/:id')
  getRun(@Param('id') id: string, @CurrentUser() user?: AuthenticatedUser): Promise<unknown> {
    const owner = this.requireUser(user);
    return this.automation.getRun(owner.id, id);
  }

  @Get('schedule-runs/:id/events')
  runEvents(
    @Param('id') id: string,
    @CurrentUser() user?: AuthenticatedUser,
    @Query('after') after?: string,
    @Query('last_event_id') lastEventIdQuery?: string,
    @Headers('last-event-id') lastEventIdHeader?: string,
  ): Promise<unknown> {
    const owner = this.requireUser(user);
    // The cursor is forwarded to the engine instead of being dropped (the
    // engine reads it from Last-Event-ID; `after` is passed as well).
    const cursor = firstCursor([after, lastEventIdQuery, lastEventIdHeader]);
    return this.automation.runEvents(owner.id, id, cursor);
  }

  @Post('schedule-runs/:id/cancel')
  @HttpCode(HttpStatus.ACCEPTED)
  cancelRun(@Param('id') id: string, @CurrentUser() user?: AuthenticatedUser): Promise<unknown> {
    const owner = this.requireUser(user);
    return this.automation.cancelRun(owner.id, id);
  }
}

/** First non-negative integer among the candidates (query before header). */
function firstCursor(candidates: Array<string | string[] | undefined>): number | null {
  for (const candidate of candidates) {
    const raw = Array.isArray(candidate) ? candidate[0] : candidate;
    if (raw === undefined || !/^\d+$/.test(raw.trim())) continue;
    return Number.parseInt(raw.trim(), 10);
  }
  return null;
}

