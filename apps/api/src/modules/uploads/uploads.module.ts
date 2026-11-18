import { Module } from '@nestjs/common';

import { UploadModule } from '../../upload/upload.module';
import { BatchesModule } from '../batches/batches.module';
import { ConversionsModule } from '../conversions/conversions.module';

import { UploadsController } from './uploads.controller';
import { UploadsService } from './uploads.service';

@Module({
  imports: [UploadModule, ConversionsModule, BatchesModule],
  controllers: [UploadsController],
  providers: [UploadsService],
  exports: [UploadsService],
})
export class UploadsModule {}
