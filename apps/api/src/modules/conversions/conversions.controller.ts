import {
  Body,
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
import type {
  Conversion,
  ConversionListItem,
  Paginated,
  Preview,
  ResultOverview,
  ResultSummary,
  SheetPage,
  SheetSourcePages,
} from '@pdf2x/contracts';

import { CurrentUser } from '../../common/decorators/current-user.decorator';
import { ListConversionsQueryDto } from '../../common/dto/pagination.dto';
import { contentDisposition } from '../../common/content-disposition';
import { ApiError } from '../../common/errors/api-error';
import { ERROR_CODES } from '../../common/errors/error-codes';
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
import { SheetListQueryDto, SheetPagesQueryDto } from './dto/sheet-query.dto';

interface CreateConversionBody {
  /** JSON string with `ConversionOptionsV2` (multipart form field). */
  options?: string | Record<string, unknown> | null;
}

/** Multipart fields arrive as strings; a JSON body may already be an object. */
function parseOptionsField(body: CreateConversionBody | undefined): unknown {
  const raw = body?.options;
  if (raw === undefined || raw === null) return undefined;
  if (typeof raw === 'object') return raw;
  if (typeof raw !== 'string' || raw.trim().length === 0) return undefined;
  try {
    return JSON.parse(raw) as unknown;
  } catch {
    throw ApiError.badRequest(ERROR_CODES.INVALID_OPTION, { reason: 'malformed_json' });
  }
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
    @Body() body: CreateConversionBody | undefined,
    @Headers('idempotency-key') idempotencyKey?: string,
  ): Promise<CreateConversionResponse> {
    if (!user) throw ApiError.unauthenticated();
    return this.conversions.create(user, file, idempotencyKey, parseOptionsField(body));
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

  /** Compact overview: same payload size for 10 cells or 10 million. */
  @Get(':id/result-summary')
  resultOverview(
    @Param('id') id: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<ResultOverview> {
    if (!user) throw ApiError.unauthenticated();
    return this.conversions.getResultOverview(user, id);
  }

  @Get(':id/sheets')
  sheets(
    @Param('id') id: string,
    @Query() query: SheetListQueryDto,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<SheetPage> {
    if (!user) throw ApiError.unauthenticated();
    return this.conversions.getSheets(user, id, query);
  }

  @Get(':id/sheets/:sheetId/pages')
  sheetPages(
    @Param('id') id: string,
    @Param('sheetId') sheetId: string,
    @Query() query: SheetPagesQueryDto,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<SheetSourcePages> {
    if (!user) throw ApiError.unauthenticated();
    return this.conversions.getSheetPages(user, id, sheetId, query);
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
