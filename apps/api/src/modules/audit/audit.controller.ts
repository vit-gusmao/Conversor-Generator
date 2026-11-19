import {
  Controller,
  Get,
  Param,
  ParseIntPipe,
  Query,
  Res,
  StreamableFile,
  UseGuards,
} from '@nestjs/common';
import type { Response } from 'express';
import type { AuditSummary, PageAudit } from '@pdf2x/contracts';

import { CurrentUser } from '../../common/decorators/current-user.decorator';
import { contentDisposition } from '../../common/content-disposition';
import { ApiError } from '../../common/errors/api-error';
import type { AuthenticatedUser } from '../../common/request-context';
import { OptionalJwtGuard } from '../../auth/guards/optional-jwt.guard';

import { AuditService, type AuditExportPayload } from './audit.service';

/**
 * Audit endpoints are a transparent proxy to the engine: the API never stores
 * audit data, it only enforces ownership. Query strings are forwarded verbatim.
 */
@Controller('conversions/:id/audit')
@UseGuards(OptionalJwtGuard)
export class AuditController {
  constructor(private readonly audit: AuditService) {}

  @Get()
  summary(
    @Param('id') id: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<AuditSummary> {
    if (!user) throw ApiError.unauthenticated();
    return this.audit.summary(user, id);
  }

  @Get('events')
  events(
    @Param('id') id: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
    @Query() query: Record<string, unknown>,
  ): Promise<{ items: unknown[]; total: number }> {
    if (!user) throw ApiError.unauthenticated();
    return this.audit.events(user, id, query);
  }

  @Get('pages/:page')
  page(
    @Param('id') id: string,
    @Param('page', ParseIntPipe) page: number,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<PageAudit> {
    if (!user) throw ApiError.unauthenticated();
    return this.audit.page(user, id, page);
  }

  @Get('cells')
  cells(
    @Param('id') id: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
    @Query() query: Record<string, unknown>,
  ): Promise<{ items: unknown[]; total: number }> {
    if (!user) throw ApiError.unauthenticated();
    return this.audit.cells(user, id, query);
  }

  @Get('issues')
  issues(
    @Param('id') id: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
    @Query() query: Record<string, unknown>,
  ): Promise<{ items: unknown[]; total: number }> {
    if (!user) throw ApiError.unauthenticated();
    return this.audit.issues(user, id, query);
  }

  @Get('export')
  async export(
    @Param('id') id: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
    @Query() query: Record<string, unknown>,
    @Res({ passthrough: true }) response: Response,
  ): Promise<StreamableFile> {
    if (!user) throw ApiError.unauthenticated();
    const payload: AuditExportPayload = await this.audit.export(user, id, query);

    response.setHeader('Content-Type', payload.contentType);
    response.setHeader('Content-Disposition', contentDisposition(payload.filename));
    response.setHeader('Cache-Control', 'no-store');
    return new StreamableFile(payload.stream);
  }
}
