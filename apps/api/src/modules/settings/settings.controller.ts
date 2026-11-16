import { Controller, Get } from '@nestjs/common';
import type { PlatformSettings } from '@pdf2x/contracts';

import { SettingsService } from './settings.service';

@Controller('settings')
export class SettingsController {
  constructor(private readonly settings: SettingsService) {}

  @Get()
  get(): Promise<PlatformSettings> {
    return this.settings.get();
  }
}
