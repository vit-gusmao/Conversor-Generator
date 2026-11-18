import 'reflect-metadata';

import { Logger } from '@nestjs/common';
import { NestFactory } from '@nestjs/core';
import { json, urlencoded, type Express, type NextFunction, type Request, type Response } from 'express';

import { AppModule } from './app.module';
import { ERROR_CODES } from './common/errors/error-codes';
import { ApiError } from './common/errors/api-error';
import { createValidationPipe } from './common/pipes/validation.pipe';
import { AppConfigService } from './config/app-config.service';
import { HealthService } from './modules/health/health.service';

async function bootstrap(): Promise<void> {
  const app = await NestFactory.create(AppModule);
  const config = app.get(AppConfigService);
  const logger = new Logger('Bootstrap');

  app.setGlobalPrefix('api/v1');
  app.enableShutdownHooks();

  // Body limits: JSON/urlencoded for normal routes, content-length pre-check
  // for multipart uploads (multer's own limit is configured in UploadModule).
  app.use(json({ limit: config.uploadMaxBytes }));
  app.use(urlencoded({ extended: true, limit: config.uploadMaxBytes }));
  app.use((request: Request, _response: Response, next: NextFunction) => {
    const raw = request.headers['content-length'];
    if (typeof raw === 'string') {
      const length = Number.parseInt(raw, 10);
      if (Number.isFinite(length) && length > config.uploadMaxBytes + 64 * 1024) {
        next(ApiError.badRequest(ERROR_CODES.FILE_TOO_LARGE, { reason: 'content_length_exceeded' }));
        return;
      }
    }
    next();
  });

  app.enableCors({
    origin: (origin: string | undefined, callback: (err: Error | null, allow?: boolean) => void) => {
      // Same-origin/non-browser requests have no Origin header.
      if (!origin) return callback(null, true);
      return callback(null, config.allowedOrigins.includes(origin));
    },
    credentials: true,
    methods: ['GET', 'POST', 'PATCH', 'PUT', 'DELETE', 'OPTIONS'],
    allowedHeaders: [
      'Authorization',
      // Sent by the SSE client (apps/web use-conversion-events.ts); not a
      // CORS-safelisted header, so it must be allowed for cross-origin previews.
      'Cache-Control',
      'Content-Type',
      'Idempotency-Key',
      'Last-Event-ID',
      'X-Device-Id',
      'X-Request-Id',
    ],
    exposedHeaders: ['X-Request-Id', 'X-Device-Id', 'Content-Disposition', 'Retry-After'],
    maxAge: 600,
  });

  app.useGlobalPipes(createValidationPipe());

  // Root alias without the global prefix (container healthchecks). It must be
  // registered before `init()` so Express matches it ahead of Nest's 404 route.
  const server = app.getHttpAdapter().getInstance() as Express;
  server.get('/health', async (_request: Request, response: Response) => {
    try {
      const report = await app.get(HealthService).check();
      response.status(200).json(report);
    } catch (error) {
      logger.error(`Falha no healthcheck: ${String(error)}`);
      response.status(503).json({
        status: 'degraded',
        services: { db: 'down', queue: 'down', storage: 'down', converter: 'down' },
      });
    }
  });

  await app.init();

  await app.listen(config.apiPort, config.apiHost);
  logger.log(`API pública em http://${config.apiHost}:${config.apiPort}/api/v1`);
  logger.log(`Modo de autenticação: ${config.authMode} | storage: ${config.storageBackend}`);
}

void bootstrap().catch((error: unknown) => {
  // eslint-disable-next-line no-console
  console.error('Falha ao iniciar a API:', error);
  process.exit(1);
});
