import {
  Body,
  Controller,
  HttpCode,
  HttpStatus,
  Post,
  UseGuards,
} from '@nestjs/common';
import { IsBoolean, IsNumber, IsObject, IsOptional, IsString, Max, MaxLength, Min } from 'class-validator';

import { ApiError } from '../../common/errors/api-error';
import { ConversionTrackerService } from './conversion-tracker.service';
import { InternalTokenGuard } from './internal-token.guard';

export class ConverterPushDto {
  @IsOptional() @IsString() @MaxLength(64) conversion_id?: string;
  @IsOptional() @IsString() @MaxLength(64) run_id?: string;
  @IsOptional() @IsString() @MaxLength(64) status?: string;
  @IsOptional() @IsString() @MaxLength(64) stage?: string;
  @IsOptional() @IsNumber() @Min(0) @Max(1) progress?: number;
  @IsOptional() @IsBoolean() progress_indeterminate?: boolean;
  @IsOptional() @IsString() @MaxLength(32) quality?: string;
  @IsOptional() @IsString() @MaxLength(1000) message?: string;
  @IsOptional() @IsString() @MaxLength(64) event_type?: string;
  @IsOptional() @IsObject() payload?: Record<string, unknown>;
  @IsOptional() @IsObject() stats?: Record<string, unknown>;
  @IsOptional() @IsObject() validation?: Record<string, unknown>;
  @IsOptional() @IsObject() error?: Record<string, unknown>;
}

/**
 * Push channel: the engine can notify status/progress instead of waiting for
 * the 800 ms poller. Same state machine, so both channels are consistent.
 */
@Controller('internal/hooks')
@UseGuards(InternalTokenGuard)
export class InternalHooksController {
  constructor(private readonly tracker: ConversionTrackerService) {}

  @Post('converter')
  @HttpCode(HttpStatus.ACCEPTED)
  async converterPush(@Body() body: ConverterPushDto): Promise<{ ok: boolean }> {
    const error = body.error
      ? {
          code: typeof body.error['code'] === 'string' ? body.error['code'] : 'unknown',
          message:
            typeof body.error['message'] === 'string'
              ? body.error['message']
              : 'Falha na conversão.',
          detail: typeof body.error['detail'] === 'string' ? body.error['detail'] : null,
          retryable: body.error['retryable'] !== false,
        }
      : undefined;

    const applied = await this.tracker.applyPush({
      conversion_id: body.conversion_id,
      run_id: body.run_id,
      status: body.status,
      stage: body.stage,
      progress: body.progress,
      progress_indeterminate: body.progress_indeterminate,
      quality: body.quality,
      message: body.message,
      event_type: body.event_type,
      payload: body.payload,
      stats: body.stats,
      validation: body.validation,
      error,
    });

    if (!applied) {
      throw ApiError.notFound({ reason: 'conversion_not_found' });
    }
    return { ok: true };
  }
}
