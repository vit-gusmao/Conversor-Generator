import { forwardRef, Module } from '@nestjs/common';

import { UploadModule } from '../../upload/upload.module';
import { BatchesModule } from '../batches/batches.module';
import { RealtimeModule } from '../realtime/realtime.module';

import { ConversionsController } from './conversions.controller';
import { ConversionsService } from './conversions.service';
import { OutboxService } from './outbox.service';

@Module({
  imports: [UploadModule, forwardRef(() => RealtimeModule), forwardRef(() => BatchesModule)],
  controllers: [ConversionsController],
  providers: [ConversionsService, OutboxService],
  exports: [ConversionsService, OutboxService],
})
export class ConversionsModule {}
