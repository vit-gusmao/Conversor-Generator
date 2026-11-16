import { Module } from '@nestjs/common';

import { UploadModule } from '../../upload/upload.module';
import { RealtimeModule } from '../realtime/realtime.module';

import { ConversionsController } from './conversions.controller';
import { ConversionsService } from './conversions.service';
import { OutboxService } from './outbox.service';

@Module({
  imports: [UploadModule, RealtimeModule],
  controllers: [ConversionsController],
  providers: [ConversionsService, OutboxService],
  exports: [ConversionsService, OutboxService],
})
export class ConversionsModule {}
