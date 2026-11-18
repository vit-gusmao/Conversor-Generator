import { Module } from '@nestjs/common';

import { ConversionsModule } from '../conversions/conversions.module';

import { BatchEventsService } from './batch-events.service';
import { BatchesController } from './batches.controller';
import { BatchesService } from './batches.service';

@Module({
  imports: [ConversionsModule],
  controllers: [BatchesController],
  providers: [BatchesService, BatchEventsService],
  exports: [BatchesService, BatchEventsService],
})
export class BatchesModule {}
