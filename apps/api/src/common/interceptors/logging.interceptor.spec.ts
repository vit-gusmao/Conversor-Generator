import { EventEmitter } from 'node:events';

import type { CallHandler, ExecutionContext } from '@nestjs/common';
import { Logger } from '@nestjs/common';
import { lastValueFrom, throwError } from 'rxjs';

import { LoggingInterceptor } from './logging.interceptor';

function contextFor(response: EventEmitter & { statusCode: number }): ExecutionContext {
  const request = { method: 'POST', originalUrl: '/api/v1/conversions', requestId: 'rid-1' };
  return {
    switchToHttp: () => ({
      getRequest: () => request,
      getResponse: () => response,
    }),
  } as unknown as ExecutionContext;
}

describe('LoggingInterceptor (A-25)', () => {
  let logSpy: jest.SpyInstance;

  beforeEach(() => {
    logSpy = jest.spyOn(Logger.prototype, 'log').mockImplementation(() => undefined);
  });

  afterEach(() => {
    logSpy.mockRestore();
  });

  it('logs the status the response finally carries, not the interceptor-time default', async () => {
    const response = Object.assign(new EventEmitter(), { statusCode: 200 });
    const interceptor = new LoggingInterceptor();
    const handler: CallHandler = {
      // A handler error: at this point the status is still the default 200.
      handle: () => throwError(() => new Error('boom')),
    };

    await expect(
      lastValueFrom(interceptor.intercept(contextFor(response), handler)),
    ).rejects.toThrow('boom');
    expect(logSpy).not.toHaveBeenCalled();

    // The exception filter sets the real code and finishes the response.
    response.statusCode = 400;
    response.emit('finish');

    expect(logSpy).toHaveBeenCalledTimes(1);
    expect(logSpy.mock.calls[0][0]).toContain('POST /api/v1/conversions 400');
  });

  it('logs once even when finish and close both fire', async () => {
    const response = Object.assign(new EventEmitter(), { statusCode: 201 });
    const interceptor = new LoggingInterceptor();
    const handler: CallHandler = { handle: () => throwError(() => new Error('x')) };
    await expect(lastValueFrom(interceptor.intercept(contextFor(response), handler))).rejects.toThrow();

    response.emit('finish');
    response.emit('close');

    expect(logSpy).toHaveBeenCalledTimes(1);
    expect(logSpy.mock.calls[0][0]).toContain(' 201 ');
  });
});
