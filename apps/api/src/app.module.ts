import { Module } from '@nestjs/common';
import { APP_FILTER, APP_INTERCEPTOR } from '@nestjs/core';

import { AuthModule } from './auth/auth.module';
import { AllExceptionsFilter } from './common/filters/all-exceptions.filter';
import { LoggingInterceptor } from './common/interceptors/logging.interceptor';
import { RequestIdInterceptor } from './common/interceptors/request-id.interceptor';
import { AppConfigModule } from './config/app-config.module';
import { DatabaseModule } from './database/database.module';
import { AuditModule } from './modules/audit/audit.module';
import { BatchesModule } from './modules/batches/batches.module';
import { ConversionsModule } from './modules/conversions/conversions.module';
import { ConverterModule } from './modules/conversions/converter.module';
import { EngineProxyModule } from './modules/engine-proxy/engine-proxy.module';
import { HealthModule } from './modules/health/health.module';
import { RealtimeModule } from './modules/realtime/realtime.module';
import { SettingsModule } from './modules/settings/settings.module';
import { UploadsModule } from './modules/uploads/uploads.module';
import { UploadModule } from './upload/upload.module';
import { AutomationModule } from './modules/automation/automation.module';

@Module({
  imports: [
    AppConfigModule,
    DatabaseModule,
    ConverterModule,
    AuthModule,
    UploadModule,
    RealtimeModule,
    ConversionsModule,
    BatchesModule,
    UploadsModule,
    EngineProxyModule,
    AutomationModule,
    AuditModule,
    SettingsModule,
    HealthModule,
  ],
  providers: [
    { provide: APP_FILTER, useClass: AllExceptionsFilter },
    // Order matters: the request id must exist before requests are logged.
    { provide: APP_INTERCEPTOR, useClass: RequestIdInterceptor },
    { provide: APP_INTERCEPTOR, useClass: LoggingInterceptor },
  ],
})
export class AppModule {}
