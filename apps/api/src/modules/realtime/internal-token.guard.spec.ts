import type { ExecutionContext } from '@nestjs/common';

import { ApiError } from '../../common/errors/api-error';
import type { AppConfigService } from '../../config/app-config.service';

import { InternalTokenGuard } from './internal-token.guard';

describe('InternalTokenGuard', () => {
  const guard = new InternalTokenGuard({ converterInternalToken: 'engine-secret' } as unknown as AppConfigService);

  const contextWith = (headers: Record<string, string | string[]>): ExecutionContext =>
    ({
      switchToHttp: () => ({ getRequest: () => ({ headers }) }),
    }) as unknown as ExecutionContext;

  it('accepts the configured internal token', () => {
    expect(guard.canActivate(contextWith({ 'x-internal-token': 'engine-secret' }))).toBe(true);
  });

  it('answers UNAUTHORIZED_INTERNAL when the token header is missing', () => {
    expect(() => guard.canActivate(contextWith({}))).toThrow(ApiError);
    try {
      guard.canActivate(contextWith({}));
    } catch (error) {
      expect(error).toMatchObject({ code: 'UNAUTHORIZED_INTERNAL', status: 401 });
    }
  });

  it('answers UNAUTHORIZED_INTERNAL when the token is wrong', () => {
    try {
      guard.canActivate(contextWith({ 'x-internal-token': 'nope' }));
      throw new Error('expected rejection');
    } catch (error) {
      expect(error).toMatchObject({
        code: 'UNAUTHORIZED_INTERNAL',
        status: 401,
        details: { reason: 'invalid_internal_token' },
      });
    }
  });
});
