import {
  Injectable,
  type CanActivate,
  type ExecutionContext,
} from '@nestjs/common';
import type { Response } from 'express';

import { ApiError } from '../../common/errors/api-error';
import type { RequestWithContext } from '../../common/request-context';
import { AppConfigService } from '../../config/app-config.service';

import { AuthService, isValidDeviceId } from '../auth.service';

function headerValue(value: string | string[] | undefined): string | undefined {
  if (Array.isArray(value)) return value[0];
  return value;
}

function cookieValue(request: RequestWithContext, name: string): string | undefined {
  const raw = headerValue(request.headers.cookie);
  if (!raw) return undefined;
  for (const part of raw.split(';')) {
    const [key, ...rest] = part.trim().split('=');
    if (key === name) {
      const value = rest.join('=');
      try {
        return decodeURIComponent(value);
      } catch {
        return value;
      }
    }
  }
  return undefined;
}

function bearerToken(request: RequestWithContext): string | null {
  const header = headerValue(request.headers.authorization);
  if (typeof header === 'string') {
    const [scheme, token] = header.split(' ');
    if (scheme?.toLowerCase() === 'bearer' && token) return token.trim();
  }
  // EventSource cannot send headers: allow ?access_token= on SSE routes.
  const query = request.query['access_token'];
  if (typeof query === 'string' && query.length > 0) return query;
  return null;
}

/**
 * The default guard for every public route.
 *
 * - valid bearer token -> that user
 * - `AUTH_MODE=required` -> 401 when the token is missing/invalid
 * - `AUTH_MODE=optional` -> deterministic device identity from `x-device-id`
 *   (a user row is created on first sight), so anonymous conversions stay
 *   private to their device.
 */
@Injectable()
export class OptionalJwtGuard implements CanActivate {
  constructor(
    private readonly auth: AuthService,
    private readonly config: AppConfigService,
  ) {}

  async canActivate(context: ExecutionContext): Promise<boolean> {
    const http = context.switchToHttp();
    const request = http.getRequest<RequestWithContext>();
    const response = http.getResponse<Response>();

    const token = bearerToken(request);
    if (token) {
      const user = await this.auth.verifyAccessToken(token);
      if (user) {
        request.user = user;
        request.deviceId = user.deviceId;
        return true;
      }
      // An explicitly supplied invalid token is never silently ignored in
      // required mode; in optional mode we fall back to the device identity.
      if (this.config.authMode === 'required') {
        throw ApiError.unauthenticated({ reason: 'invalid_token' });
      }
    } else if (this.config.authMode === 'required') {
      throw ApiError.unauthenticated({ reason: 'missing_token' });
    }

    const headerDeviceId = headerValue(request.headers['x-device-id']);
    const queryDeviceId = request.query['device_id'];
    const cookieDeviceId = cookieValue(request, 'pdf2x_device_id');
    let deviceId = isValidDeviceId(headerDeviceId)
      ? headerDeviceId
      : isValidDeviceId(queryDeviceId)
        ? queryDeviceId
        : isValidDeviceId(cookieDeviceId)
          ? cookieDeviceId
          : null;

    if (!deviceId) {
      deviceId = this.auth.newDeviceId();
      // Lets browser clients persist the identity on their first anonymous hit.
      response.setHeader('x-device-id', deviceId);
    }

    const user = await this.auth.getOrCreateDeviceUser(deviceId);
    request.deviceId = deviceId;
    request.user = this.auth.toAuthenticated(user);
    return true;
  }
}
