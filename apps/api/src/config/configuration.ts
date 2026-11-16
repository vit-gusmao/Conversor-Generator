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
  databaseUrl: string;
  jwtSecret: string;
  jwtExpiresIn: string;
  authMode: AuthMode;
  uploadMaxBytes: number;
  uploadMaxPages: number;
  allowedOrigins: string[];
  publicApiUrl: string;
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

export function configuration(): AppConfiguration {
  const authMode = readEnum<AuthMode>('AUTH_MODE', ['optional', 'required'], 'optional');
  const runtimeMode = readEnum<RuntimeMode>('RUNTIME_MODE', ['standalone', 'full'], 'full');
  const storageBackend = readEnum<StorageBackend>('STORAGE_BACKEND', ['local', 's3'], 'local');
  const logLevel = readEnum<LogLevel>('LOG_LEVEL', ['debug', 'info', 'warn', 'error'], 'info');

  return {
    runtimeMode,
    engineVersion: readString('ENGINE_VERSION', '1.0.0'),
    logLevel,
    api: {
      port: readInt('API_PORT', 3000, 1),
      databaseUrl: readString('API_DATABASE_URL', 'file:./data/api.db'),
      jwtSecret: readString('JWT_SECRET', 'dev-only-insecure-secret-change-me'),
      jwtExpiresIn: readString('JWT_EXPIRES_IN', '7d'),
      authMode,
      uploadMaxBytes: readInt('UPLOAD_MAX_BYTES', 52428800, 1),
      uploadMaxPages: readInt('UPLOAD_MAX_PAGES', 500, 1),
      allowedOrigins: readList('ALLOWED_ORIGINS', [
        'http://localhost:5173',
        'http://localhost:3000',
      ]),
      publicApiUrl: readString('PUBLIC_API_URL', 'http://localhost:3000'),
    },
    converter: {
      internalUrl: readString('CONVERTER_INTERNAL_URL', 'http://localhost:8000'),
      internalToken: readString('CONVERTER_INTERNAL_TOKEN', 'dev-internal-token-change-me'),
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
