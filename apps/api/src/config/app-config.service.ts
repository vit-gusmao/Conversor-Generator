import { Injectable } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';

import type {
  ApiSection,
  AppConfiguration,
  AuthMode,
  ConverterSection,
  FeatureSection,
  LogLevel,
  RetentionSection,
  RuntimeMode,
  S3Section,
  StorageBackend,
  StorageSection,
  UploadSection,
} from './configuration';

/**
 * Thin typed facade over `@nestjs/config`.
 *
 * Services never read `process.env` directly and never deal with string ->
 * number coercion; they depend on this class.
 */
@Injectable()
export class AppConfigService {
  private readonly _api: ApiSection;
  private readonly _upload: UploadSection;
  private readonly _features: FeatureSection;
  private readonly _converter: ConverterSection;
  private readonly _storage: StorageSection;
  private readonly _retention: RetentionSection;
  private readonly _runtimeMode: RuntimeMode;
  private readonly _engineVersion: string;
  private readonly _logLevel: LogLevel;

  constructor(configService: ConfigService) {
    const api = configService.get<ApiSection>('api');
    const upload = configService.get<UploadSection>('upload');
    const features = configService.get<FeatureSection>('features');
    const converter = configService.get<ConverterSection>('converter');
    const storage = configService.get<StorageSection>('storage');
    const retention = configService.get<RetentionSection>('retention');

    if (!api || !upload || !features || !converter || !storage || !retention) {
      throw new Error('Configuração da aplicação não carregada');
    }

    this._api = api;
    this._upload = upload;
    this._features = features;
    this._converter = converter;
    this._storage = storage;
    this._retention = retention;
    this._runtimeMode = configService.get<RuntimeMode>('runtimeMode') ?? 'full';
    this._engineVersion = configService.get<string>('engineVersion') ?? '1.0.0';
    this._logLevel = configService.get<LogLevel>('logLevel') ?? 'info';
  }

  get apiPort(): number {
    return this._api.port;
  }

  get apiHost(): string {
    return this._api.host;
  }

  get databaseUrl(): string {
    return this._api.databaseUrl;
  }

  get jwtSecret(): string {
    return this._api.jwtSecret;
  }

  get jwtExpiresIn(): string {
    return this._api.jwtExpiresIn;
  }

  get authMode(): AuthMode {
    return this._api.authMode;
  }

  get uploadMaxBytes(): number {
    return this._api.uploadMaxBytes;
  }

  /** `null` means no ceiling: the whole document is processed. */
  get uploadMaxPages(): number | null {
    return this._api.uploadMaxPages;
  }

  get upload(): UploadSection {
    return this._upload;
  }

  get uploadPartBytes(): number {
    return this._upload.partBytes;
  }

  get uploadPartMaxBytes(): number {
    return this._upload.partMaxBytes;
  }

  get uploadConcurrency(): number {
    return this._upload.concurrency;
  }

  get maxFilesPerBatch(): number | null {
    return this._upload.maxFilesPerBatch;
  }

  get maxFileBytes(): number | null {
    return this._upload.maxFileBytes;
  }

  get uploadSessionTtlHours(): number {
    return this._upload.sessionTtlHours;
  }

  get features(): FeatureSection {
    return this._features;
  }

  get batchUploadEnabled(): boolean {
    return this._features.batchUpload;
  }

  get largeDocumentPipelineEnabled(): boolean {
    return this._features.largeDocumentPipeline;
  }

  get resultCompactEnabled(): boolean {
    return this._features.resultCompact;
  }

  get mcpEnabled(): boolean {
    return this._features.mcp;
  }

  get allowedOrigins(): string[] {
    return this._api.allowedOrigins;
  }

  get publicApiUrl(): string {
    return this._api.publicApiUrl;
  }

  get converterInternalUrl(): string {
    return this._converter.internalUrl.replace(/\/+$/, '');
  }

  get converterInternalToken(): string {
    return this._converter.internalToken;
  }

  get converterTimeoutMs(): number {
    return this._converter.timeoutMs;
  }

  get converterPollIntervalMs(): number {
    return this._converter.pollIntervalMs;
  }

  get converterHealthCacheMs(): number {
    return this._converter.healthCacheMs;
  }

  get storageBackend(): StorageBackend {
    return this._storage.backend;
  }

  get storageLocalRoot(): string {
    return this._storage.localRoot;
  }

  get s3(): S3Section {
    return this._storage.s3;
  }

  get retention(): RetentionSection {
    return this._retention;
  }

  get runtimeMode(): RuntimeMode {
    return this._runtimeMode;
  }

  get engineVersion(): string {
    return this._engineVersion;
  }

  get logLevel(): LogLevel {
    return this._logLevel;
  }

  /** Read-only snapshot, useful for diagnostics and tests. */
  snapshot(): AppConfiguration {
    return {
      runtimeMode: this._runtimeMode,
      engineVersion: this._engineVersion,
      logLevel: this._logLevel,
      api: this._api,
      upload: this._upload,
      features: this._features,
      converter: this._converter,
      storage: this._storage,
      retention: this._retention,
    };
  }
}
