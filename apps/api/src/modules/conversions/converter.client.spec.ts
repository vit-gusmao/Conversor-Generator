import { AppConfigService } from '../../config/app-config.service';
import { captureApiErrorAsync } from '../../../test/helpers/test-utils';

import { ConverterClient, type DownloadPayload } from './converter.client';

type FetchMock = jest.Mock<Promise<Response>, [string, RequestInit?]>;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

describe('ConverterClient', () => {
  let fetchMock: FetchMock;
  let client: ConverterClient;

  const config = {
    converterInternalUrl: 'http://engine.local/',
    converterInternalToken: 'token-interno',
    converterTimeoutMs: 25,
  } as unknown as AppConfigService;

  beforeEach(() => {
    fetchMock = jest.fn();
    client = new ConverterClient(config, fetchMock);
  });

  it('enqueues a job with the internal token and snake_case payload', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ accepted: true, run_id: 'run-1', status: 'queued' }));

    const result = await client.enqueue({
      runId: 'run-1',
      conversionId: 'conv-1',
      storageKey: 'ab/hash.pdf',
      filename: 'holerite.pdf',
      options: { maxPages: 50, ocr: true },
    });

    expect(result).toEqual({ accepted: true, run_id: 'run-1', status: 'queued' });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('http://engine.local/internal/v1/jobs/run-1/enqueue');
    expect(init?.method).toBe('POST');
    expect((init?.headers as Record<string, string>)['x-internal-token']).toBe('token-interno');

    const payload = JSON.parse(String(init?.body)) as Record<string, unknown>;
    expect(payload).toMatchObject({
      run_id: 'run-1',
      conversion_id: 'conv-1',
      storage_key: 'ab/hash.pdf',
      filename: 'holerite.pdf',
      options: { max_pages: 50, ocr: true },
    });
  });

  it('maps a timeout to 503 CONVERTER_UNAVAILABLE', async () => {
    fetchMock.mockImplementation(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => {
            const error = new Error('The operation was aborted');
            error.name = 'AbortError';
            reject(error);
          });
        }),
    );

    const error = await captureApiErrorAsync(() => client.status('run-timeout'));

    expect(error.status).toBe(503);
    expect(error.code).toBe('CONVERTER_UNAVAILABLE');
    expect(error.details).toMatchObject({ reason: 'timeout', timeout_ms: 25 });
  });

  it('maps an upstream 503 to 503 CONVERTER_UNAVAILABLE', async () => {
    fetchMock.mockResolvedValue(new Response('engine offline', { status: 503 }));

    const error = await captureApiErrorAsync(() => client.status('run-1'));

    expect(error.status).toBe(503);
    expect(error.code).toBe('CONVERTER_UNAVAILABLE');
    expect(error.details).toMatchObject({ upstream_status: 503 });
  });

  it('maps an upstream 404 to NOT_FOUND and 409 to NOT_READY', async () => {
    fetchMock.mockResolvedValueOnce(new Response('missing', { status: 404 }));
    const notFound = await captureApiErrorAsync(() => client.result('run-x'));
    expect(notFound.status).toBe(404);

    fetchMock.mockResolvedValueOnce(new Response('not ready', { status: 409 }));
    const notReady = await captureApiErrorAsync(() => client.download('run-x'));
    expect(notReady.status).toBe(409);
    expect(notReady.code).toBe('NOT_READY');
  });

  it('maps network failures to 503', async () => {
    fetchMock.mockRejectedValue(new Error('ECONNREFUSED'));

    const error = await captureApiErrorAsync(() => client.status('run-1'));

    expect(error.status).toBe(503);
    expect(error.details).toMatchObject({ reason: 'network_error' });
  });

  it('normalises a status document', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        run_id: 'run-1',
        status: 'extracting',
        stage: 'extract',
        progress: 0.42,
        progress_indeterminate: false,
        quality: 'pending',
        message: 'Extraindo OCR',
        stats: { pages_total: 3 },
        error: null,
        events: [{ type: 'log', message: 'página 1', payload: { page: 1 } }],
      }),
    );

    const state = await client.status('run-1');

    expect(state).toMatchObject({
      run_id: 'run-1',
      status: 'extracting',
      stage: 'extract',
      progress: 0.42,
      message: 'Extraindo OCR',
    });
    expect(state.stats).toMatchObject({ pages_total: 3 });
    expect(state.events).toHaveLength(1);
    expect(fetchMock.mock.calls[0][0]).toBe('http://engine.local/internal/v1/jobs/run-1');
  });

  it('streams the download with engine headers', async () => {
    fetchMock.mockResolvedValue(
      new Response(Buffer.from('xlsx-bytes'), {
        status: 200,
        headers: {
          'content-type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          'content-disposition': 'attachment; filename="folha.xlsx"',
          'content-length': '10',
        },
      }),
    );

    const payload: DownloadPayload = await client.download('run-1');

    expect(payload.filename).toBe('folha.xlsx');
    expect(payload.contentLength).toBe(10);
    expect(payload.contentType).toContain('spreadsheetml');
    payload.stream.destroy();
  });

  it('never throws from safeHealth', async () => {
    fetchMock.mockRejectedValue(new Error('offline'));
    await expect(client.safeHealth()).resolves.toBeNull();
  });

  it('parses health from the internal endpoint', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ status: 'ok', engine_version: '2.1.0', ocr_enabled: true, capabilities: { ocr: true } }),
    );

    const health = await client.health();

    expect(fetchMock.mock.calls[0][0]).toBe('http://engine.local/internal/v1/health');
    expect(health).toMatchObject({
      status: 'ok',
      engine_version: '2.1.0',
      ocr_enabled: true,
      capabilities: { ocr: true },
    });
  });
});
