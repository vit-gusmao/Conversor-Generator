/**
 * Entry point.
 *
 * stdio is the default transport: stdout carries only MCP protocol frames, so
 * every diagnostic goes to stderr. `http` serves the same tools over Streamable
 * HTTP at `/mcp` with a `/health` probe.
 */

import { createServer as createHttpServer } from 'node:http';
import { Readable } from 'node:stream';

import { createMcpHandler } from '@modelcontextprotocol/server';
import { StdioServerTransport } from '@modelcontextprotocol/server/stdio';

import { ApiClient } from './api-client.js';
import { ConfigError, loadConfig, type McpConfig } from './config.js';
import { resolvePrincipal } from './identity.js';
import { createMcpServer } from './server.js';
import { SubmissionStore } from './submission-store.js';
import { TransferWorker, revalidateLocalSources } from './transfer-worker.js';

function log(message: string): void {
  process.stderr.write(`[pdf2x-mcp] ${message}\n`);
}

async function buildRuntime(config: McpConfig) {
  const principal = await resolvePrincipal(config);
  const store = await SubmissionStore.open(config.configDir, { log });

  const api = new ApiClient({
    baseUrl: principal.apiBaseUrl,
    deviceId: principal.deviceId,
    log: (message) => log(message),
  });

  const worker = new TransferWorker({
    api,
    store,
    allowedRoots: config.allowedRoots,
    concurrency: config.transferConcurrency,
    log: (message) => log(message),
  });

  // Local sources are re-validated (existence and size) before any resume; the
  // SHA-256 is recomputed lazily by the worker when it actually resumes.
  await revalidateLocalSources(store, config.allowedRoots, log);
  await worker.resumeIncomplete();

  return { store, api, worker };
}

async function runStdio(config: McpConfig): Promise<void> {
  const { store, api, worker } = await buildRuntime(config);
  const server = createMcpServer(config, api, store, { log, worker });
  const transport = new StdioServerTransport();

  const shutdown = async (): Promise<void> => {
    try {
      await server.close();
    } catch {
      /* already closed */
    }
    await store.close();
    process.exit(0);
  };
  process.once('SIGINT', () => void shutdown());
  process.once('SIGTERM', () => void shutdown());

  await server.connect(transport);
  log(`ready on stdio (api ${config.apiBaseUrl})`);
}

async function runHttp(config: McpConfig): Promise<void> {
  const { store, api, worker } = await buildRuntime(config);
  const handler = createMcpHandler(() => createMcpServer(config, api, store, { log, worker }), {
    onerror: (error) => log(`http transport error: ${error.message}`),
  });

  const httpServer = createHttpServer(async (request, response) => {
    try {
      const url = new URL(request.url ?? '/', `http://${request.headers.host ?? 'localhost'}`);

      if (url.pathname === '/health') {
        response.writeHead(200, { 'content-type': 'application/json' });
        response.end(JSON.stringify({ status: 'ok', transport: 'http', name: 'pdf2x-mcp' }));
        return;
      }
      if (url.pathname !== '/mcp' && !url.pathname.startsWith('/mcp/')) {
        response.writeHead(404, { 'content-type': 'application/json' });
        response.end(JSON.stringify({ error: { code: 'NOT_FOUND', message: 'Use /mcp.' } }));
        return;
      }

      const headers = new Headers();
      for (const [name, value] of Object.entries(request.headers)) {
        if (typeof value === 'string') headers.set(name, value);
        else if (Array.isArray(value)) headers.set(name, value.join(', '));
      }

      const init: RequestInit & { duplex?: 'half' } = {
        method: request.method,
        headers,
      };
      if (request.method !== 'GET' && request.method !== 'HEAD') {
        init.body = Readable.toWeb(request) as unknown as ReadableStream;
        init.duplex = 'half';
      }

      const mcpRequest = new Request(url.toString(), init);
      const mcpResponse = await handler.fetch(mcpRequest);
      response.writeHead(mcpResponse.status, Object.fromEntries(mcpResponse.headers.entries()));
      if (!mcpResponse.body) {
        response.end();
        return;
      }
      Readable.fromWeb(mcpResponse.body as Parameters<typeof Readable.fromWeb>[0]).pipe(response);
    } catch (error) {
      log(`http request failed: ${(error as Error).message}`);
      if (!response.headersSent) {
        response.writeHead(500, { 'content-type': 'application/json' });
      }
      response.end(
        JSON.stringify({ error: { code: 'INTERNAL_ERROR', message: 'Request failed.' } }),
      );
    }
  });

  await new Promise<void>((resolve, reject) => {
    httpServer.once('error', reject);
    httpServer.listen(config.httpPort, config.httpHost, () => resolve());
  });
  log(`ready on http://${config.httpHost}:${config.httpPort}/mcp`);

  const shutdown = async (): Promise<void> => {
    httpServer.close();
    await handler.close();
    await store.close();
    process.exit(0);
  };
  process.once('SIGINT', () => void shutdown());
  process.once('SIGTERM', () => void shutdown());
}

async function main(): Promise<void> {
  let config: McpConfig;
  try {
    config = loadConfig();
  } catch (error) {
    if (error instanceof ConfigError) {
      log(`configuration error: ${error.message}`);
      process.exit(2);
    }
    throw error;
  }

  try {
    if (config.transport === 'http') await runHttp(config);
    else await runStdio(config);
  } catch (error) {
    log(`fatal: ${(error as Error).message}`);
    process.exit(1);
  }
}

void main();
