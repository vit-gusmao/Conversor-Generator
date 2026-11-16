import { Module } from '@nestjs/common';
import { MulterModule } from '@nestjs/platform-express';

import { AppConfigService } from '../config/app-config.service';

import { FileValidationService, UPLOAD_LIMITS, type UploadLimits } from './file-validation.service';
import { LocalStorageService } from './local-storage.service';
import { S3StorageService } from './s3-storage.service';
import { STORAGE_SERVICE, type StorageService } from './storage.service';

@Module({
  imports: [
    // Applies to every FileInterceptor in importing modules: an upload larger
    // than UPLOAD_MAX_BYTES is aborted by multer before touching the service.
    MulterModule.registerAsync({
      inject: [AppConfigService],
      useFactory: (config: AppConfigService) => ({
        limits: { fileSize: config.uploadMaxBytes, files: 1, fields: 10 },
      }),
    }),
  ],
  providers: [
    {
      provide: UPLOAD_LIMITS,
      inject: [AppConfigService],
      useFactory: (config: AppConfigService): UploadLimits => ({
        maxBytes: config.uploadMaxBytes,
        maxPages: config.uploadMaxPages,
      }),
    },
    FileValidationService,
    {
      provide: STORAGE_SERVICE,
      inject: [AppConfigService],
      useFactory: (config: AppConfigService): StorageService =>
        config.storageBackend === 's3'
          ? new S3StorageService(config)
          : new LocalStorageService(config),
    },
  ],
  exports: [FileValidationService, STORAGE_SERVICE, MulterModule, UPLOAD_LIMITS],
})
export class UploadModule {}
