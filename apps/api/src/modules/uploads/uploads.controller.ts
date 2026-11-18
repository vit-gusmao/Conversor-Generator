import {
  Body,
  Controller,
  Delete,
  Get,
  Headers,
  HttpCode,
  HttpStatus,
  Param,
  ParseIntPipe,
  Post,
  Put,
  Query,
  Req,
  UseGuards,
} from '@nestjs/common';
import type { CompleteUploadResponse, UploadPartReceipt, UploadSessionDetail } from '@pdf2x/contracts';
import type { Request } from 'express';

import { OptionalJwtGuard } from '../../auth/guards/optional-jwt.guard';
import { CurrentUser } from '../../common/decorators/current-user.decorator';
import { ApiError } from '../../common/errors/api-error';
import type { AuthenticatedUser } from '../../common/request-context';

import { CompleteUploadDto, UploadPartsQueryDto } from './dto/upload.dto';
import { UploadsService } from './uploads.service';

@Controller('uploads')
@UseGuards(OptionalJwtGuard)
export class UploadsController {
  constructor(private readonly uploads: UploadsService) {}

  /**
   * Receives one part. The body is consumed as a stream; `Content-Length` is a
   * transport hint, and the part hash may be sent as `x-part-sha256`.
   */
  @Put(':id/parts/:part')
  @HttpCode(HttpStatus.OK)
  putPart(
    @Param('id') id: string,
    @Param('part', ParseIntPipe) part: number,
    @Req() request: Request,
    @CurrentUser() user: AuthenticatedUser | undefined,
    @Headers('x-part-sha256') declaredSha256?: string,
    @Headers('content-length') contentLength?: string,
  ): Promise<UploadPartReceipt> {
    if (!user) throw ApiError.unauthenticated();
    const length = contentLength && /^\d+$/.test(contentLength) ? Number(contentLength) : null;
    return this.uploads.putPart(user, id, part, request, declaredSha256, length);
  }

  @Get(':id')
  get(
    @Param('id') id: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
    @Query() query: UploadPartsQueryDto,
  ): Promise<UploadSessionDetail> {
    if (!user) throw ApiError.unauthenticated();
    return this.uploads.get(user, id, query);
  }

  @Get(':id/parts')
  listParts(
    @Param('id') id: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
    @Query() query: UploadPartsQueryDto,
  ): Promise<UploadSessionDetail> {
    if (!user) throw ApiError.unauthenticated();
    return this.uploads.get(user, id, query);
  }

  @Post(':id/complete')
  @HttpCode(HttpStatus.ACCEPTED)
  complete(
    @Param('id') id: string,
    @Body() body: CompleteUploadDto,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<CompleteUploadResponse> {
    if (!user) throw ApiError.unauthenticated();
    return this.uploads.complete(user, id, body.sha256, body.total_parts);
  }

  @Delete(':id')
  @HttpCode(HttpStatus.OK)
  abort(
    @Param('id') id: string,
    @CurrentUser() user: AuthenticatedUser | undefined,
  ): Promise<Record<string, unknown>> {
    if (!user) throw ApiError.unauthenticated();
    return this.uploads.abort(user, id);
  }
}
