/**
 * Error type shared by the source/schedule/runtime contract helpers.
 * ============================================================================
 * Mirrors the Python `ApiError` envelope codes introduced by the upgrade
 * plan (T1). Conversion options keep their dedicated `ConversionOptionsError`.
 */

export type ContractErrorCode =
  | 'UNKNOWN_FIELD'
  | 'INVALID_PAYLOAD'
  | 'INVALID_SOURCE'
  | 'INVALID_SCHEDULE_RULE'
  | 'INVALID_TIMEZONE';

export class ContractError extends Error {
  readonly code: ContractErrorCode;
  readonly details: Record<string, unknown>;

  constructor(code: ContractErrorCode, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.name = 'ContractError';
    this.code = code;
    this.details = details;
  }
}
