import { Module } from '@nestjs/common';

import { ConversionEventBus } from './conversion-event-bus.service';
import { ConversionEventsService } from './conversion-events.service';
import { ConversionEventsController } from './conversion-events.controller';
import { ConversionTrackerService } from './conversion-tracker.service';
import { InternalHooksController } from './internal-hooks.controller';
import { InternalTokenGuard } from './internal-token.guard';

@Module({
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
