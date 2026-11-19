import { forwardRef, Module } from '@nestjs/common';

import { BatchesModule } from '../batches/batches.module';

import { ConversionEventBus } from './conversion-event-bus.service';
import { ConversionEventsService } from './conversion-events.service';
import { ConversionEventsController } from './conversion-events.controller';
import { ConversionTrackerService } from './conversion-tracker.service';
import { InternalHooksController } from './internal-hooks.controller';
import { InternalTokenGuard } from './internal-token.guard';

@Module({
  imports: [forwardRef(() => BatchesModule)],
  controllers: [ConversionEventsController, InternalHooksController],
  providers: [
    ConversionEventBus,
    ConversionEventsService,
    ConversionTrackerService,
    InternalTokenGuard,
  ],
  exports: [ConversionEventBus, ConversionEventsService, ConversionTrackerService],
})
export class RealtimeModule {}
