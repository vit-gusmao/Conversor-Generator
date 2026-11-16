import { ApiError } from '../../common/errors/api-error';

import { statusOf, type ConversionWithRelations } from './conversions.mapper';

/**
 * A conversion is private to its owner. Existence is checked before ownership
 * so a deleted id returns 404 and a foreign id returns 403 (per docs/API.md).
 */
export function assertOwnership(
  conversion: ConversionWithRelations,
  ownerId: string,
): void {
  if (conversion.deletedAt) throw ApiError.notFound({ resource: 'conversion' });
  if (conversion.ownerId !== ownerId) throw ApiError.forbidden({ resource: 'conversion' });
}

export function assertNotTerminal(conversion: ConversionWithRelations): void {
  if (['completed', 'completed_with_warnings', 'failed', 'cancelled'].includes(statusOf(conversion))) {
    throw ApiError.conflict('ALREADY_TERMINAL');
  }
}
