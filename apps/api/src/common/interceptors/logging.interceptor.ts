import {
  CallHandler,
  ExecutionContext,
  Injectable,
  Logger,
  type NestInterceptor,
} from '@nestjs/common';
import type { Response } from 'express';
import type { Observable } from 'rxjs';

import type { RequestWithContext } from '../request-context';

/**
 * One line per request. Never logs bodies or file contents (LGPD: PDFs may
 * carry CPF, salary and other personal data). Query-string credentials used by
 * the SSE fallback are redacted before the line is written.
 */
const SENSITIVE_QUERY =
  /([?&](?:access_token|device_id|ticket|token|api_key|apikey|secret|password)=)[^&#]*/gi;

export function redactUrl(url: string): string {
  const [path, fragment] = url.split('#', 2);
  const redacted = path.replace(SENSITIVE_QUERY, '$1[REDACTED]');
  return fragment ? `${redacted}#[REDACTED]` : redacted;
}

@Injectable()
export class LoggingInterceptor implements NestInterceptor {
  private readonly logger = new Logger('HTTP');

  intercept(context: ExecutionContext, next: CallHandler): Observable<unknown> {
    const http = context.switchToHttp();
    const request = http.getRequest<RequestWithContext>();
    const response = http.getResponse<Response>();
    const startedAt = Date.now();

    // The status is only final once the response has been written. At
    // interceptor time an error still carries the default 200, because the
    // exception filter runs later and sets the real code (A-25). So the line
    // is written on 'finish' (or 'close' when the client went away first).
    let logged = false;
    const write = (): void => {
      if (logged) return;
      logged = true;
      const duration = Date.now() - startedAt;
      this.logger.log(
        `${request.method} ${redactUrl(request.originalUrl)} ${response.statusCode} ${duration}ms rid=${request.requestId ?? '-'}`,
      );
    };
    response.once('finish', write);
    response.once('close', write);

    return next.handle();
  }
}
