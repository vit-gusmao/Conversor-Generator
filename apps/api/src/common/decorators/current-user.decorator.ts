import { createParamDecorator, type ExecutionContext } from '@nestjs/common';

import type { AuthenticatedUser, RequestWithContext } from '../request-context';

/** Injects the authenticated (or device-identified) user. */
export const CurrentUser = createParamDecorator(
  (_data: unknown, context: ExecutionContext): AuthenticatedUser | undefined => {
    const request = context.switchToHttp().getRequest<RequestWithContext>();
    return request.user;
  },
);
