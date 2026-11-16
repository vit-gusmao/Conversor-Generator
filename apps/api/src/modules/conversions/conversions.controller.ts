import {
  Controller,
  Delete,
  Get,
  Headers,
  HttpCode,
  HttpStatus,
  Param,
  Post,
  Query,
  Res,
  StreamableFile,
  UploadedFile,
  UseGuards,
  UseInterceptors,
} from '@nestjs/common';
import { FileInterceptor } from '@nestjs/platform-express';
import type { Response } from 'express';
import type { Conversion, Paginated, ConversionListItem, Preview, ResultSummary } from '@pdf2x/contracts';

import { CurrentUser } from '../../common/decorators/current-user.decorator';
import { ListConversionsQueryDto } from '../../common/dto/pagination.dto';
import { ApiError } from '../../common/errors/api-error';
import type { AuthenticatedUser } from '../../common/request-context';
import { OptionalJwtGuard } from '../../auth/guards/optional-jwt.guard';

import { ConversionsService } from './conversions.service';
import type { PageListResponse, TableListResponse } from './converter.client';
import type {
  CancelConversionResponse,
  CreateConversionResponse,
  UploadedPdfFile,
} from './dto/conversion-response.dto';
import { PreviewQueryDto } from './dto/preview-query.dto';

function contentDisposition(filename: string): string {
  const ascii = filename.replace(/[^\x20-\x7e]/g, '_').replace(/["\\]/g, '_');
  const encoded = encodeURIComponent(filename);
  return `attachment; filename="${ascii}"; filename*=UTF-8''${encoded}`;
}

@Controller('conversions')
@UseGuards(OptionalJwtGuard)
export class ConversionsController {
  constructor(private readonly conversions: ConversionsService) {}

  @Post()
  @HttpCode(HttpStatus.ACCEPTED)
  @UseInterceptors(FileInterceptor('file'))
  async create(
    @UploadedFile() file: UploadedPdfFile | undefined,
    @CurrentUser() user: AuthenticatedUser | undefined,
    @Headers('idempotency-key') idempotencyKey?: string,
  ): Promise<CreateConversionResponse> {
    if (!user) throw ApiError.unauthenticated();
    return this.conversions.create(user, file, idempotencyKey);
  }

  @Get()
  list(
    @CurrentUser() user: AuthenticatedUser | undefined,
    @Query() query: ListConversionsQueryDto,
  ): Promise<Paginated<ConversionListItem>> {
    if (!user) throw ApiError.unauthenticated();
    return this.conversions.list(user, query);
  }

  @Get(':id')
  detail(
    @Param('id') id: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<Conversion> {
    if (!user) throw ApiError.unauthenticated();
    return this.conversions.getDetailResponse(user, id);
  }

  @Get(':id/result')
  result(
    @Param('id') id: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<ResultSummary> {
    if (!user) throw ApiError.unauthenticated();
    return this.conversions.getResult(user, id);
  }

  @Get(':id/preview')
  preview(
    @Param('id') id: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
    @Query() query: PreviewQueryDto,
  ): Promise<Preview> {
    if (!user) throw ApiError.unauthenticated();
    return this.conversions.getPreview(user, id, query);
  }

  @Get(':id/pages')
  pages(
    @Param('id') id: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<PageListResponse> {
    if (!user) throw ApiError.unauthenticated();
    return this.conversions.getPages(user, id);
  }

  @Get(':id/tables')
  tables(
    @Param('id') id: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<TableListResponse> {
    if (!user) throw ApiError.unauthenticated();
    return this.conversions.getTables(user, id);
  }

  @Get(':id/cells/:cellId')
  cellEvidence(
    @Param('id') id: string,
    @Param('cellId') cellId: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<Record<string, unknown>> {
    if (!user) throw ApiError.unauthenticated();
    return this.conversions.getCellEvidence(user, id, cellId);
  }

  @Get(':id/download')
  async download(
    @Param('id') id: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
    @Res({ passthrough: true }) response: Response,
  ): Promise<StreamableFile> {
    if (!user) throw ApiError.unauthenticated();
    const payload = await this.conversions.getDownload(user, id);

    response.setHeader('Content-Type', payload.contentType);
    response.setHeader('Content-Disposition', contentDisposition(payload.filename));
    response.setHeader('Cache-Control', 'no-store');
    response.setHeader('X-Content-Type-Options', 'nosniff');
    if (payload.contentLength) {
      response.setHeader('Content-Length', String(payload.contentLength));
    }

    return new StreamableFile(payload.stream);
  }

  @Post(':id/retry')
  @HttpCode(HttpStatus.ACCEPTED)
  retry(
    @Param('id') id: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<CreateConversionResponse> {
    if (!user) throw ApiError.unauthenticated();
    return this.conversions.retry(user, id);
  }

  @Post(':id/cancel')
  @HttpCode(HttpStatus.OK)
  cancel(
    @Param('id') id: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<CancelConversionResponse> {
    if (!user) throw ApiError.unauthenticated();
    return this.conversions.cancel(user, id);
  }

  @Delete(':id')
  @HttpCode(HttpStatus.NO_CONTENT)
  async remove(
    @Param('id') id: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<void> {
    if (!user) throw ApiError.unauthenticated();
    await this.conversions.remove(user, id);
  }
}
