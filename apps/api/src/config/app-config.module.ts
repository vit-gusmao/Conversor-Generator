import { Global, Module } from '@nestjs/common';
import { ConfigModule } from '@nestjs/config';

import { AppConfigService } from './app-config.service';
import { configuration } from './configuration';

/**
 * Monorepo root `.env` first, then an optional local override next to the app.
 * `ignoreEnvFile` is intentionally false so `docker run -e ...` still wins
 * because real environment variables have precedence over files.
 */
@Global()
@Module({
  imports: [
    ConfigModule.forRoot({
      isGlobal: true,
      cache: true,
      expandVariables: true,
      envFilePath: ['../../.env', '.env'],
      load: [configuration],
    }),
  ],
  providers: [AppConfigService],
  exports: [AppConfigService, ConfigModule],
})
export class AppConfigModule {}
