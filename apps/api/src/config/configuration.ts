/**
 * Typed environment configuration.
 *
 * Every value has a safe default so the API boots with no .env at all.
 * `@nestjs/config` loads `../../.env` first and then a local `.env`,
 * so the monorepo root file wins (same file every service reads).
 */

export type AuthMode = 'optional' | 'required';
export type RuntimeMode = 'standalone' | 'full';
export type StorageBackend = 'local' | 's3';
export type LogLevel = 'debug' | 'info' | 'warn' | 'error';

export interface ApiSection {
  port: number;
  /** Interface the HTTP server binds to. Loopback unless overridden (containers use 0.0.0.0). */
  host: string;
  databaseUrl: string;
  jwtSecret: string;
  jwtExpiresIn: string;
  authMode: AuthMode;
  /** Limit of the legacy single-shot multipart endpoint, in bytes. */
  uploadMaxBytes: number;
  /**
   * @deprecated No page ceiling exists in the default flow. Only set this to
   * a number to keep an operator-imposed cap on the *legacy* endpoint; `null`
   * means documents are processed in full.
   */
  uploadMaxPages: number | null;
  allowedOrigins: string[];
  publicApiUrl: string;
}

/** Resumable upload / batch feature settings. */
export interface UploadSection {
  /** Default part size. A transport parameter, not a document ceiling. */
  partBytes: number;
  /** Largest accepted single part. */
  partMaxBytes: number;
  /** Client-side parallelism the server advertises. */
  concurrency: number;
  /** `null` means "no product ceiling on files per batch". */
  maxFilesPerBatch: number | null;
  /** `null` means "no product ceiling on document bytes" for the new path. */
  maxFileBytes: number | null;
  /** How long an incomplete session stays resumable. */
  sessionTtlHours: number;
  /** Reject a new session when a single document would exceed this. */
  maxDocumentBytes: number | null;
}

/** Rollout flags. Disabling admission never interrupts runs already started. */
export interface FeatureSection {
  batchUpload: boolean;
  largeDocumentPipeline: boolean;
  resultCompact: boolean;
  mcp: boolean;
}

export interface ConverterSection {
  internalUrl: string;
  internalToken: string;
  timeoutMs: number;
  pollIntervalMs: number;
  healthCacheMs: number;
}

export interface S3Section {
  endpoint: string | null;
  region: string;
  accessKeyId: string | null;
  secretAccessKey: string | null;
  bucket: string;
  forcePathStyle: boolean;
  secure: boolean;
}

export interface StorageSection {
  backend: StorageBackend;
  localRoot: string;
  s3: S3Section;
}

export interface RetentionSection {
  pdfDays: number;
  xlsxDays: number;
  auditDays: number;
}

export interface AppConfiguration {
  runtimeMode: RuntimeMode;
  engineVersion: string;
  logLevel: LogLevel;
  api: ApiSection;
  upload: UploadSection;
  features: FeatureSection;
  converter: ConverterSection;
  storage: StorageSection;
  retention: RetentionSection;
}


function env(name: string): string | undefined {
  const value = process.env[name];
  if (value === undefined) return undefined;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : undefined;
}

export function readString(name: string, fallback: string): string {
  return env(name) ?? fallback;
}

export function readOptionalString(name: string): string | null {
  return env(name) ?? null;
}

export function readInt(name: string, fallback: number, min = 0): number {
  const raw = env(name);
  if (raw === undefined) return fallback;
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed) || parsed < min) return fallback;
  return parsed;
}

/**
 * Reads an optional integer. Explicitly setting the variable to `0`, `null`,
 * `none` or an empty value means "no limit" and returns `null`.
 */
export function readOptionalInt(name: string, fallback: number | null = null, min = 0): number | null {
  const raw = env(name)?.toLowerCase();
  if (raw === undefined) return fallback;
  if (raw === '0' || raw === 'null' || raw === 'none' || raw === 'off') return null;
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed) || parsed < min) return fallback;
  return parsed;
}

export function readFloat(name: string, fallback: number): number {
  const raw = env(name);
  if (raw === undefined) return fallback;
  const parsed = Number.parseFloat(raw);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function readBool(name: string, fallback: boolean): boolean {
  const raw = env(name)?.toLowerCase();
  if (raw === undefined) return fallback;
  return ['1', 'true', 'yes', 'on'].includes(raw);
}

export function readList(name: string, fallback: string[]): string[] {
  const raw = env(name);
  if (raw === undefined) return fallback;
  const items = raw
    .split(',')
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
  return items.length > 0 ? items : fallback;
}

export function readEnum<T extends string>(name: string, allowed: readonly T[], fallback: T): T {
  const raw = env(name) as T | undefined;
  if (raw !== undefined && (allowed as readonly string[]).includes(raw)) return raw;
  return fallback;
}

/** Development fallbacks. They must never serve traffic in production (A-02). */
export const DEV_JWT_SECRET = 'dev-only-insecure-secret-change-me';
export const DEV_INTERNAL_TOKEN = 'dev-internal-token-change-me';

/**
 * In production a dev secret is a startup error. Elsewhere it is a warning,
 * like the Python engine does. Tests (`NODE_ENV=test`) stay quiet.
 */
function assertRuntimeSecrets(jwtSecret: string, internalToken: string): void {
  const insecure = [
    ...(jwtSecret === DEV_JWT_SECRET ? ['JWT_SECRET'] : []),
    ...(internalToken === DEV_INTERNAL_TOKEN ? ['CONVERTER_INTERNAL_TOKEN'] : []),
  ];
  if (insecure.length === 0) return;

  const names = insecure.join(', ');
  const nodeEnv = env('NODE_ENV');
  if (nodeEnv === 'production') {
    throw new Error(
      `Segredos com valor padrão de desenvolvimento em NODE_ENV=production (${names}). ` +
        'Defina-os antes de iniciar a API.',
    );
  }
  if (nodeEnv !== 'test') {
    // Plain console line: this runs before the Nest logger exists.
    console.warn(
      `[configuration] segredos com valor padrão de desenvolvimento em uso (${names}): ` +
        'defina-os antes de expor o serviço',
    );
  }
}

export function configuration(): AppConfiguration {
  const authMode = readEnum<AuthMode>('AUTH_MODE', ['optional', 'required'], 'optional');
  const runtimeMode = readEnum<RuntimeMode>('RUNTIME_MODE', ['standalone', 'full'], 'full');
  const storageBackend = readEnum<StorageBackend>('STORAGE_BACKEND', ['local', 's3'], 'local');
  const logLevel = readEnum<LogLevel>('LOG_LEVEL', ['debug', 'info', 'warn', 'error'], 'info');

  const partBytes = readInt('UPLOAD_PART_BYTES', 8 * 1024 * 1024, 64 * 1024);
  const partMaxBytes = Math.max(partBytes, readInt('UPLOAD_PART_MAX_BYTES', 32 * 1024 * 1024, 64 * 1024));

  const jwtSecret = readString('JWT_SECRET', DEV_JWT_SECRET);
  const internalToken = readString('CONVERTER_INTERNAL_TOKEN', DEV_INTERNAL_TOKEN);
  assertRuntimeSecrets(jwtSecret, internalToken);

  return {
    runtimeMode,
    engineVersion: readString('ENGINE_VERSION', '1.0.0'),
    logLevel,
    api: {
      port: readInt('API_PORT', 3000, 1),
      host: readString('API_HOST', '127.0.0.1'),
      // Runtime datasource. API_DATABASE_URL wins; API_POSTGRES_URL is used only
      // when the SQLite one is unset (the Postgres deployment sets only that one).
      databaseUrl:
        readOptionalString('API_DATABASE_URL') ??
        readOptionalString('API_POSTGRES_URL') ??
        'file:./data/api.db',
      jwtSecret,
      jwtExpiresIn: readString('JWT_EXPIRES_IN', '7d'),
      authMode,
      uploadMaxBytes: readInt('UPLOAD_MAX_BYTES', 52428800, 1),
      // `null` by default: full-document processing is the product behaviour.
      uploadMaxPages: readOptionalInt('UPLOAD_MAX_PAGES', null, 1),
      allowedOrigins: readList('ALLOWED_ORIGINS', [
        'http://localhost:5173',
        'http://localhost:3000',
      ]),
      publicApiUrl: readString('PUBLIC_API_URL', 'http://localhost:3000'),
    },
    upload: {
      partBytes,
      partMaxBytes,
      concurrency: readInt('UPLOAD_CONCURRENCY', 3, 1),
      maxFilesPerBatch: readOptionalInt('MAX_FILES_PER_BATCH', null, 1),
      maxFileBytes: readOptionalInt('UPLOAD_MAX_FILE_BYTES', null, 1),
      sessionTtlHours: readInt('UPLOAD_SESSION_TTL_HOURS', 24, 1),
      maxDocumentBytes: readOptionalInt('UPLOAD_MAX_DOCUMENT_BYTES', null, 1),
    },
    features: {
      batchUpload: readBool('BATCH_UPLOAD_ENABLED', true),
      largeDocumentPipeline: readBool('LARGE_DOCUMENT_PIPELINE_ENABLED', true),
      resultCompact: readBool('RESULT_COMPACT_ENABLED', true),
      mcp: readBool('MCP_ENABLED', true),
    },
    converter: {
      internalUrl: readString('CONVERTER_INTERNAL_URL', 'http://localhost:8000'),
      internalToken,
      timeoutMs: readInt('CONVERTER_TIMEOUT_MS', 30000, 1000),
      pollIntervalMs: readInt('CONVERTER_POLL_INTERVAL_MS', 800, 100),
      healthCacheMs: readInt('CONVERTER_HEALTH_CACHE_MS', 30000, 1000),
    },
    storage: {
      backend: storageBackend,
      localRoot: readString('STORAGE_LOCAL_ROOT', './data/storage'),
      s3: {
        endpoint: readOptionalString('S3_ENDPOINT'),
        region: readString('S3_REGION', 'us-east-1'),
        accessKeyId: readOptionalString('S3_ACCESS_KEY'),
        secretAccessKey: readOptionalString('S3_SECRET_KEY'),
        bucket: readString('S3_BUCKET', 'pdf2x'),
        forcePathStyle: readBool('S3_FORCE_PATH_STYLE', true),
        secure: readBool('S3_SECURE', false),
      },
    },
    retention: {
      pdfDays: readInt('RETENTION_PDF_DAYS', 7),
      xlsxDays: readInt('RETENTION_XLSX_DAYS', 30),
      auditDays: readInt('RETENTION_AUDIT_DAYS', 90),
    },
  };
}
