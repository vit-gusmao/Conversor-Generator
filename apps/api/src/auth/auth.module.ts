import { Global, Module } from '@nestjs/common';
import { JwtModule } from '@nestjs/jwt';
import { PassportModule } from '@nestjs/passport';

import { AppConfigService } from '../config/app-config.service';

import { AuthController } from './auth.controller';
import { AuthService } from './auth.service';
import { JwtAuthGuard } from './guards/jwt-auth.guard';
import { OptionalJwtGuard } from './guards/optional-jwt.guard';
import { JwtStrategy } from './strategies/jwt.strategy';

/**
 * Global on purpose: every feature module needs `OptionalJwtGuard`, and Nest
 * resolves guard dependencies in the injector of the consuming module.
 */
@Global()
@Module({
  imports: [
    PassportModule.register({ defaultStrategy: 'jwt' }),
    JwtModule.registerAsync({
      inject: [AppConfigService],
      // `expiresIn` is applied per sign call in AuthService (numeric seconds).
      useFactory: (config: AppConfigService) => ({ secret: config.jwtSecret }),
    }),
  ],
  controllers: [AuthController],
  providers: [AuthService, OptionalJwtGuard, JwtAuthGuard, JwtStrategy],
  exports: [AuthService, JwtModule, OptionalJwtGuard, JwtAuthGuard],
})
export class AuthModule {}
