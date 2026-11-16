import { ValidationPipe, type ValidationError } from '@nestjs/common';

import { ApiError } from '../errors/api-error';

export function flattenValidationErrors(errors: ValidationError[]): string[] {
  return errors.flatMap((error) => [
    ...Object.values(error.constraints ?? {}),
    ...flattenValidationErrors(error.children ?? []),
  ]);
}

/**
 * Shared pipe configuration so `main.ts` and the e2e bootstrap cannot drift.
 * Invalid payloads become `422 VALIDATION_ERROR` (docs/API.md), not Nest's 400.
 */
export function createValidationPipe(): ValidationPipe {
  return new ValidationPipe({
    whitelist: true,
    transform: true,
    forbidUnknownValues: false,
    validationError: { target: false, value: false },
    exceptionFactory: (errors: ValidationError[]) =>
      ApiError.validation({ errors: flattenValidationErrors(errors) }),
  });
}
