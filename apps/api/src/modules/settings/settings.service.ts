import { Injectable, Logger } from '@nestjs/common';
import type { PlatformSettings } from '@pdf2x/contracts';

import { AppConfigService } from '../../config/app-config.service';
import { ConverterClient, type ConverterHealth } from '../conversions/converter.client';

/**
 * `GET /settings` — everything the UI needs to configure itself.
 * The engine is probed at most once per HEALTH_CACHE_MS (default 30 s).
 */
@Injectable()
export class SettingsService {
  private readonly logger = new Logger(SettingsService.name);
  private cached: { at: number; value: PlatformSettings } | null = null;

  constructor(
    private readonly config: AppConfigService,
    private readonly converter: ConverterClient,
  ) {}

  async get(force = false): Promise<PlatformSettings> {
    const now = Date.now();
    if (!force && this.cached && now - this.cached.at < this.config.converterHealthCacheMs) {
      return this.cached.value;
    }

    const health = await this.converter.safeHealth();
    const value = this.build(health);
    this.cached = { at: now, value };
    if (!health) {
      this.logger.debug('Motor indisponível ao montar /settings; capabilities reduzidas');
    }
    return value;
  }

  private build(health: ConverterHealth | null): PlatformSettings {
    const reachable = health !== null;
    const capabilities: Record<string, boolean> = {
      converter: reachable,
      ocr: health?.ocr_enabled ?? false,
      tables: reachable,
      audit: reachable,
      preview: reachable,
      download: reachable,
      sse: true,
      push_events: true,
      ...(health?.capabilities ?? {}),
    };

    return {
      auth_mode: this.config.authMode,
      max_upload_bytes: this.config.uploadMaxBytes,
      max_pages: this.config.uploadMaxPages,
      runtime_mode: this.config.runtimeMode,
      ocr_enabled: health?.ocr_enabled ?? false,
      ocr_engine: health?.ocr_engine ?? null,
      storage_backend: this.config.storageBackend,
      retention: {
        pdf_days: this.config.retention.pdfDays,
        xlsx_days: this.config.retention.xlsxDays,
        audit_days: this.config.retention.auditDays,
      },
      engine_version: health?.engine_version ?? this.config.engineVersion,
      capabilities,
    };
  }
}
