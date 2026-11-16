import { Module } from '@nestjs/common';

import { EngineProxyController } from './engine-proxy.controller';
import { EngineProxyService } from './engine-proxy.service';

@Module({
  controllers: [EngineProxyController],
  providers: [EngineProxyService],
})
export class EngineProxyModule {}
