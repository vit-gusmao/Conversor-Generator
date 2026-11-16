import {
  ArgumentsHost,
  Catch,
  HttpException,
  HttpStatus,
  Logger,
  type ExceptionFilter,
} from '@nestjs/common';
import type { Response } from 'express';

import { ApiError } from '../errors/api-error';
import { ERROR_CODES, ERROR_MESSAGES, type ErrorCode } from '../errors/error-codes';
import type { RequestWithContext } from '../request-context';

interface ErrorBody {
  code: ErrorCode;
  message: string;
  details?: Record<string, unknown>;
}

interface ValidationLikeResponse {
  message?: string | string[];
  error?: string;
  statusCode?: number;
  details?: Record<string, unknown>;
}

const STATUS_TO_CODE: Record<number, ErrorCode> = {
  [HttpStatus.BAD_REQUEST]: ERROR_CODES.VALIDATION_ERROR,
  [HttpStatus.UNAUTHORIZED]: ERROR_CODES.UNAUTHENTICATED,
  [HttpStatus.FORBIDDEN]: ERROR_CODES.FORBIDDEN,
  [HttpStatus.NOT_FOUND]: ERROR_CODES.NOT_FOUND,
  [HttpStatus.CONFLICT]: ERROR_CODES.ALREADY_TERMINAL,
  [HttpStatus.UNPROCESSABLE_ENTITY]: ERROR_CODES.VALIDATION_ERROR,
  [HttpStatus.TOO_MANY_REQUESTS]: ERROR_CODES.RATE_LIMITED,
  [HttpStatus.SERVICE_UNAVAILABLE]: ERROR_CODES.CONVERTER_UNAVAILABLE,
};

/**
 * Single exit point for every error: `{ error: { code, message, details } }`.
 * Unexpected failures are logged with the stack and never leaked to clients.
 */
@Catch()
export class AllExceptionsFilter implements ExceptionFilter {
  private readonly logger = new Logger('ExceptionFilter');

  catch(exception: unknown, host: ArgumentsHost): void {
    const ctx = host.switchToHttp();
    const response = ctx.getResponse<Response>();
    const request = ctx.getRequest<RequestWithContext>();
    const requestId = request?.requestId;

    const { status, error } = this.resolve(exception);

    if (status >= 500) {
      this.logger.error(
        `${request?.method ?? '-'} ${request?.originalUrl ?? '-'} -> ${status} ${error.code}`,
        exception instanceof Error ? exception.stack : String(exception),
      );
    } else if (status >= 400) {
      this.logger.warn(
        `${request?.method ?? '-'} ${request?.originalUrl ?? '-'} -> ${status} ${error.code}: ${error.message}`,
      );
    }

    // Headers may already be flushed when an SSE stream fails.
    if (response.headersSent) {
      if (!response.writableEnded) response.end();
      return;
    }

    const body: { error: ErrorBody & { request_id?: string } } = {
      error: { ...error, ...(requestId ? { request_id: requestId } : {}) },
    };
    response.status(status).json(body);
  }

  private resolve(exception: unknown): { status: number; error: ErrorBody } {
    if (exception instanceof ApiError) {
      return {
        status: exception.status,
        error: {
          code: exception.code,
          message: exception.message,
          ...(exception.details ? { details: exception.details } : {}),
        },
      };
    }

    if (this.isMulterError(exception)) {
      const isSize = exception.code === 'LIMIT_FILE_SIZE';
      const code = isSize ? ERROR_CODES.FILE_TOO_LARGE : ERROR_CODES.INVALID_FILE;
      return {
        status: HttpStatus.BAD_REQUEST,
        error: { code, message: ERROR_MESSAGES[code], details: { field: exception.field } },
      };
    }

    if (exception instanceof HttpException) {
      const status = exception.getStatus();
      const payload = exception.getResponse();
      const code = STATUS_TO_CODE[status] ?? ERROR_CODES.INTERNAL;

      if (typeof payload === 'string') {
        return { status, error: { code, message: this.translate(payload, code) } };
      }

      const body = payload as ValidationLikeResponse;
      if (Array.isArray(body.message)) {
        return {
          status,
          error: {
            code: ERROR_CODES.VALIDATION_ERROR,
            message: ERROR_MESSAGES.VALIDATION_ERROR,
            details: { errors: body.message },
          },
        };
      }

      const message =
        typeof body.message === 'string' && body.message.length > 0
          ? this.translate(body.message, code)
          : ERROR_MESSAGES[code];

      return {
        status,
        error: {
          code,
          message,
          ...(body.details ? { details: body.details } : {}),
        },
      };
    }

    return {
      status: HttpStatus.INTERNAL_SERVER_ERROR,
      error: { code: ERROR_CODES.INTERNAL, message: ERROR_MESSAGES.INTERNAL },
    };
  }

  /** Nest/passport default messages are English; replace them with pt-BR. */
  private translate(message: string, code: ErrorCode): string {
    const english = new Set([
      'Unauthorized',
      'Forbidden',
      'Not Found',
      'Bad Request',
      'Conflict',
      'Internal Server Error',
      'Service Unavailable',
    ]);
    return english.has(message) ? ERROR_MESSAGES[code] : message;
  }

  private isMulterError(value: unknown): value is { name: 'MulterError'; code: string; field?: string } {
    return (
      typeof value === 'object' &&
      value !== null &&
      (value as { name?: string }).name === 'MulterError' &&
      typeof (value as { code?: unknown }).code === 'string'
    );
  }
}
