import {
  Body,
  Controller,
  Get,
  HttpCode,
  HttpStatus,
  Post,
  Req,
  Res,
  UseGuards,
} from '@nestjs/common';
import type { AuthSession, User as ContractUser } from '@pdf2x/contracts';
import type { Response } from 'express';

import { CurrentUser } from '../common/decorators/current-user.decorator';
import { ApiError } from '../common/errors/api-error';
import type { AuthenticatedUser, RequestWithContext } from '../common/request-context';

import { AuthService, isValidDeviceId } from './auth.service';
import { AnonymousSessionDto } from './dto/auth.dto';
import { OptionalJwtGuard } from './guards/optional-jwt.guard';

const DEVICE_COOKIE = 'pdf2x_device_id';

@Controller('auth')
export class AuthController {
  constructor(private readonly auth: AuthService) {}

  /**
   * Local session: no registration, login or password. One owner per browser,
   * kept by `x-device-id` and an HttpOnly cookie.
   */
  @Post('anonymous')
  @HttpCode(HttpStatus.CREATED)
  anonymous(
    @Body() dto: AnonymousSessionDto,
    @Req() request: RequestWithContext,
    @Res({ passthrough: true }) response: Response,
  ): Promise<AuthSession> {
    const headerDeviceId = request.headers['x-device-id'];
    const provided =
      dto.device_id ?? (Array.isArray(headerDeviceId) ? headerDeviceId[0] : headerDeviceId);
    const deviceId = isValidDeviceId(provided) ? provided : this.auth.newDeviceId();
    response.cookie(DEVICE_COOKIE, deviceId, {
      httpOnly: true,
      sameSite: 'lax',
      maxAge: 31_536_000_000,
      path: '/',
    });
    response.setHeader('x-device-id', deviceId);
    response.setHeader('access-control-expose-headers', 'x-device-id');
    return this.auth.createAnonymousSession(deviceId);
  }

  @Get('me')
  @UseGuards(OptionalJwtGuard)
  me(@CurrentUser() user: AuthenticatedUser | undefined): ContractUser {
    if (!user) throw ApiError.unauthenticated();
    return {
      id: user.id,
      email: user.email,
      name: user.name,
      role: user.role,
      is_anonymous: user.isAnonymous,
      created_at: user.createdAt,
    };
  }
}
