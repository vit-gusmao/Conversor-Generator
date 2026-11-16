import { Injectable } from '@nestjs/common';
import { PassportStrategy } from '@nestjs/passport';
import { ExtractJwt, Strategy } from 'passport-jwt';

import { ApiError } from '../../common/errors/api-error';
import type { AuthenticatedUser } from '../../common/request-context';
import { AppConfigService } from '../../config/app-config.service';

import { AuthService, type JwtPayload } from '../auth.service';
import { JwtAuthGuard } from '../guards/jwt-auth.guard';

/** Strategy behind {@link JwtAuthGuard}. */
@Injectable()
export class JwtStrategy extends PassportStrategy(Strategy, 'jwt') {
  constructor(
    config: AppConfigService,
    private readonly auth: AuthService,
  ) {
    super({
      jwtFromRequest: ExtractJwt.fromAuthHeaderAsBearerToken(),
      ignoreExpiration: false,
      secretOrKey: config.jwtSecret,
    });
  }

  async validate(payload: JwtPayload): Promise<AuthenticatedUser> {
    const user = await this.auth.findById(payload.sub);
    if (!user) throw ApiError.unauthenticated({ reason: 'user_not_found' });
    return this.auth.toAuthenticated(user);
  }
}
