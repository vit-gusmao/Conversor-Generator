import { Global, Module } from '@nestjs/common';

import { ConverterClient } from './converter.client';

/** Global so conversions, realtime, audit, settings and health share it. */
@Global()
@Module({
  providers: [ConverterClient],
  exports: [ConverterClient],
})
export class ConverterModule {}
