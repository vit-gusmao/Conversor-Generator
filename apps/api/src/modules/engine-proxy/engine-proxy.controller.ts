import {
  Body,
  Controller,
  Delete,
  Get,
  HttpCode,
  HttpStatus,
  Param,
  Post,
  Query,
  Res,
  UseGuards,
} from '@nestjs/common';
import type { Response } from 'express';

import { CurrentUser } from '../../common/decorators/current-user.decorator';
import { ApiError } from '../../common/errors/api-error';
import type { AuthenticatedUser } from '../../common/request-context';
import { OptionalJwtGuard } from '../../auth/guards/optional-jwt.guard';

import { EngineProxyService } from './engine-proxy.service';

function requireUser(user: AuthenticatedUser | undefined): AuthenticatedUser {
  if (!user) throw ApiError.unauthenticated();
  return user;
}

/**
 * Parity routes for the engine features that are not part of the legacy
 * PDF→XLSX gateway contract: capabilities, TXT/CSV artifacts, knowledge base
 * and the local assistant.  The engine remains the owner of the data; the
 * gateway authenticates the user and forwards an owner id it has verified.
 */
@Controller()
@UseGuards(OptionalJwtGuard)
export class EngineProxyController {
  constructor(private readonly proxy: EngineProxyService) {}

  @Get('capabilities')
  capabilities(): Promise<unknown> {
    return this.proxy.json('/internal/v1/capabilities');
  }

  @Get('conversions/:id/exports')
  async listExports(
    @Param('id') id: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<unknown> {
    const owner = requireUser(user);
    const runId = await this.proxy.runIdFor(owner.id, id);
    return this.proxy.json(`/internal/v1/runs/${encodeURIComponent(runId)}/exports`);
  }

  @Post('conversions/:id/exports')
  @HttpCode(HttpStatus.CREATED)
  async createExport(
    @Param('id') id: string,
    @Body() body: { format?: string; options?: Record<string, unknown> },
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<unknown> {
    const owner = requireUser(user);
    const runId = await this.proxy.runIdFor(owner.id, id);
    return this.proxy.json(`/internal/v1/runs/${encodeURIComponent(runId)}/exports`, {
      method: 'POST',
      body: { format: body?.format, options: body?.options ?? {} },
    });
  }

  @Get('conversions/:id/exports/:exportId/download')
  async downloadExport(
    @Param('id') id: string,
    @Param('exportId') exportId: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
    @Res() res: Response,
  ): Promise<void> {
    const owner = requireUser(user);
    const runId = await this.proxy.runIdFor(owner.id, id);
    const file = await this.proxy.binary(
      `/internal/v1/runs/${encodeURIComponent(runId)}/exports/${encodeURIComponent(exportId)}/download`,
    );
    res.status(200);
    res.setHeader('Content-Type', file.contentType);
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader(
      'Content-Disposition',
      `attachment; filename="${file.filename ?? 'exportacao'}"`,
    );
    res.send(file.body);
  }

  @Get('conversions/:id/text')
  async text(
    @Param('id') id: string,
    @Query('page') page: string | undefined,
    @Query('offset') offset: string | undefined,
    @Query('limit') limit: string | undefined,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<unknown> {
    const owner = requireUser(user);
    const runId = await this.proxy.runIdFor(owner.id, id);
    const search = new URLSearchParams();
    if (page) search.set('page', page);
    if (offset) search.set('offset', offset);
    if (limit) search.set('limit', limit);
    const query = search.toString();
    return this.proxy.json(
      `/internal/v1/runs/${encodeURIComponent(runId)}/text${query ? `?${query}` : ''}`,
    );
  }

  @Get('models/status')
  modelStatus(): Promise<unknown> {
    return this.proxy.json('/internal/v1/models/status');
  }

  /* ------------------------------------------------------------------ */
  /* Knowledge                                                          */
  /* ------------------------------------------------------------------ */

  @Get('knowledge/collections')
  async collections(@CurrentUser() user: AuthenticatedUser | undefined): Promise<unknown> {
    const owner = requireUser(user);
    return this.proxy.json('/internal/v1/knowledge/collections', { ownerId: owner.id });
  }

  @Post('knowledge/collections')
  @HttpCode(HttpStatus.CREATED)
  async createCollection(
    @Body() body: unknown,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<unknown> {
    const owner = requireUser(user);
    return this.proxy.json('/internal/v1/knowledge/collections', {
      method: 'POST',
      body,
      ownerId: owner.id,
    });
  }

  @Delete('knowledge/collections/:collectionId')
  @HttpCode(HttpStatus.NO_CONTENT)
  async deleteCollection(
    @Param('collectionId') collectionId: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
    @Res() res: Response,
  ): Promise<void> {
    const owner = requireUser(user);
    await this.proxy.json(
      `/internal/v1/knowledge/collections/${encodeURIComponent(collectionId)}`,
      { method: 'DELETE', ownerId: owner.id },
    );
    res.status(204).send();
  }

  @Post('knowledge/index-jobs')
  @HttpCode(HttpStatus.ACCEPTED)
  async createIndexJob(
    @Body() body: unknown,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<unknown> {
    const owner = requireUser(user);
    return this.proxy.json('/internal/v1/knowledge/index-jobs', {
      method: 'POST',
      body,
      ownerId: owner.id,
    });
  }

  @Get('knowledge/index-jobs/:jobId')
  async indexJob(
    @Param('jobId') jobId: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<unknown> {
    const owner = requireUser(user);
    return this.proxy.json(`/internal/v1/knowledge/index-jobs/${encodeURIComponent(jobId)}`, {
      ownerId: owner.id,
    });
  }

  /* ------------------------------------------------------------------ */
  /* Assistant                                                          */
  /* ------------------------------------------------------------------ */

  @Get('chat/conversations')
  async conversations(@CurrentUser() user: AuthenticatedUser | undefined): Promise<unknown> {
    const owner = requireUser(user);
    return this.proxy.json('/internal/v1/chat/conversations', { ownerId: owner.id });
  }

  @Post('chat/conversations')
  @HttpCode(HttpStatus.CREATED)
  async createConversation(
    @Body() body: unknown,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<unknown> {
    const owner = requireUser(user);
    return this.proxy.json('/internal/v1/chat/conversations', {
      method: 'POST',
      body,
      ownerId: owner.id,
    });
  }

  @Get('chat/conversations/:conversationId')
  async conversation(
    @Param('conversationId') conversationId: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<unknown> {
    const owner = requireUser(user);
    return this.proxy.json(
      `/internal/v1/chat/conversations/${encodeURIComponent(conversationId)}`,
      { ownerId: owner.id },
    );
  }

  @Delete('chat/conversations/:conversationId')
  @HttpCode(HttpStatus.NO_CONTENT)
  async deleteConversation(
    @Param('conversationId') conversationId: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
    @Res() res: Response,
  ): Promise<void> {
    const owner = requireUser(user);
    await this.proxy.json(
      `/internal/v1/chat/conversations/${encodeURIComponent(conversationId)}`,
      { method: 'DELETE', ownerId: owner.id },
    );
    res.status(204).send();
  }

  @Post('chat/conversations/:conversationId/messages')
  async chat(
    @Param('conversationId') conversationId: string,
    @Body() body: unknown,
    @CurrentUser() user: AuthenticatedUser | undefined,
    @Res() res: Response,
  ): Promise<void> {
    const owner = requireUser(user);
    await this.proxy.stream(
      res,
      `/internal/v1/chat/conversations/${encodeURIComponent(conversationId)}/messages`,
      { method: 'POST', body, ownerId: owner.id },
    );
  }
}
