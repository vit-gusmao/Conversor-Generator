import { ERROR_CODES, ERROR_MESSAGES, type ErrorCode } from './error-codes';

export interface ApiErrorPayload {
  error: {
    code: ErrorCode;
    message: string;
    details?: Record<string, unknown>;
    request_id?: string;
  };
}

/**
 * Domain error carrying the public `code`, the HTTP status and pt-BR message.
 * The global exception filter serialises it as `{ error: { code, message } }`.
 */
export class ApiError extends Error {
  readonly code: ErrorCode;
  readonly status: number;
  readonly details?: Record<string, unknown>;
  readonly expose = true;

  constructor(
    code: ErrorCode,
    status: number,
    message?: string,
    details?: Record<string, unknown>,
  ) {
    super(message ?? ERROR_MESSAGES[code]);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.details = details;
    Error.captureStackTrace?.(this, ApiError);
  }

  static badRequest(code: ErrorCode, details?: Record<string, unknown>): ApiError {
    return new ApiError(code, 400, ERROR_MESSAGES[code], details);
  }

  static unauthenticated(details?: Record<string, unknown>): ApiError {
    return new ApiError(ERROR_CODES.UNAUTHENTICATED, 401, undefined, details);
  }

  static invalidCredentials(): ApiError {
    return new ApiError(ERROR_CODES.INVALID_CREDENTIALS, 401);
  }

  static forbidden(details?: Record<string, unknown>): ApiError {
    return new ApiError(ERROR_CODES.FORBIDDEN, 403, undefined, details);
  }

  static notFound(details?: Record<string, unknown>): ApiError {
    return new ApiError(ERROR_CODES.NOT_FOUND, 404, undefined, details);
  }

  static conflict(code: ErrorCode, details?: Record<string, unknown>): ApiError {
    return new ApiError(code, 409, ERROR_MESSAGES[code], details);
  }

  static validation(details?: Record<string, unknown>): ApiError {
    return new ApiError(ERROR_CODES.VALIDATION_ERROR, 422, undefined, details);
  }

  static internal(details?: Record<string, unknown>): ApiError {
    return new ApiError(ERROR_CODES.INTERNAL, 500, undefined, details);
  }

  static converterUnavailable(details?: Record<string, unknown>): ApiError {
    return new ApiError(ERROR_CODES.CONVERTER_UNAVAILABLE, 503, undefined, details);
  }

  static storageUnavailable(details?: Record<string, unknown>): ApiError {
    return new ApiError(ERROR_CODES.STORAGE_UNAVAILABLE, 503, undefined, details);
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError;
}
