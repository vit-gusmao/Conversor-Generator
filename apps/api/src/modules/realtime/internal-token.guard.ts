import {
  Injectable,
  type CanActivate,
  type ExecutionContext,
} from '@nestjs/common';
import { timingSafeEqual } from 'node:crypto';
import type { Request } from 'express';

import { ApiError } from '../../common/errors/api-error';
import { AppConfigService } from '../../config/app-config.service';

function safeEquals(a: string, b: string): boolean {
  const bufferA = Buffer.from(a, 'utf8');
  const bufferB = Buffer.from(b, 'utf8');
  if (bufferA.length !== bufferB.length) return false;
  return timingSafeEqual(bufferA, bufferB);
}

/** Guards engine -> API callbacks with `x-internal-token`. */
@Injectable()
export class InternalTokenGuard implements CanActivate {
  constructor(private readonly config: AppConfigService) {}

  canActivate(context: ExecutionContext): boolean {
    const request = context.switchToHttp().getRequest<Request>();
    const header = request.headers['x-internal-token'];
    const token = Array.isArray(header) ? header[0] : header;

    if (!token) {
      throw ApiError.unauthenticated({ reason: 'missing_internal_token' });
    }
    if (!safeEquals(token, this.config.converterInternalToken)) {
      throw ApiError.unauthenticated({ reason: 'invalid_internal_token' });
    }
    return true;
  }
}
