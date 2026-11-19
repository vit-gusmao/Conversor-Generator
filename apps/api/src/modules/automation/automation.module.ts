import { Module } from '@nestjs/common';

import { BatchesModule } from '../batches/batches.module';
import { ConversionsModule } from '../conversions/conversions.module';
import { EngineProxyModule } from '../engine-proxy/engine-proxy.module';
import { UploadModule } from '../../upload/upload.module';
import { AutomationController } from './automation.controller';
import { AutomationInternalController } from './automation-internal.controller';
import { AutomationService } from './automation.service';

/**
 * Full-mode automation proxy (T6). The engine stays the single writer of
 * sources/imports/schedules; this module forwards owner-authenticated calls
 * and accepts engine submissions over the internal token.
 */
@Module({
  imports: [EngineProxyModule, BatchesModule, ConversionsModule, UploadModule],
  controllers: [AutomationController, AutomationInternalController],
  providers: [AutomationService],
  exports: [AutomationService],
})
export class AutomationModule {}
