/**
 * Test harness: a real stdio MCP client talking to the built server, backed by
 * the in-process mock API.
 *
 * The client negotiates the protocol through the official SDK, so the tests
 * exercise the same entry point a host uses (`dist/main.js`), not direct handler
 * calls.
 */

import { createHash } from 'node:crypto';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { Client } from '@modelcontextprotocol/client';
import { StdioClientTransport } from '@modelcontextprotocol/client/stdio';
import type { CallToolResult } from '@modelcontextprotocol/server';

import { MockApi, type MockOptions } from './mock-api.js';

const here = path.dirname(fileURLToPath(import.meta.url));
export const appRoot = path.resolve(here, '..', '..');
export const serverEntry = path.join(appRoot, 'dist', 'main.js');

export interface Harness {
  client: Client;
  mock: MockApi;
  root: string;
  configDir: string;
  outsideDir: string;
  baseDir: string;
  env: Record<string, string>;
  call(name: string, args: Record<string, unknown>): Promise<CallToolResult>;
  restart(): Promise<void>;
  close(): Promise<void>;
}

export interface HarnessOptions {
  mock?: Partial<MockOptions>;
  env?: Record<string, string>;
}

export interface CallOutcome {
  isError: boolean;
  structured: Record<string, unknown>;
  error: { code: string; message: string; details: Record<string, unknown>; status?: number } | null;
  text: string;
}

export function outcome(result: CallToolResult): CallOutcome {
  const structured = (result.structuredContent ?? {}) as Record<string, unknown>;
  const error = (structured.error ?? null) as CallOutcome['error'];
  const first = result.content?.[0];
  return {
    isError: result.isError === true,
    structured,
    error,
    text: first && first.type === 'text' ? first.text : '',
  };
}

function environment(overrides: Record<string, string>): Record<string, string> {
  const env: Record<string, string> = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (typeof value === 'string') env[key] = value;
  }
  return { ...env, ...overrides };
}

async function startClient(env: Record<string, string>): Promise<{ client: Client; transport: StdioClientTransport }> {
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [serverEntry],
    cwd: appRoot,
    env,
    stderr: 'inherit',
  });
  const client = new Client({ name: 'pdf2x-mcp-tests', version: '0.0.0' });
  await client.connect(transport);
  return { client, transport };
}

export async function createHarness(options: HarnessOptions = {}): Promise<Harness> {
  const baseDir = await fs.mkdtemp(path.join(os.tmpdir(), 'pdf2x-mcp-test-'));
  const root = path.join(baseDir, 'root');
  const configDir = path.join(baseDir, 'config');
  const outsideDir = path.join(baseDir, 'outside');
  await fs.mkdir(root, { recursive: true });
  await fs.mkdir(configDir, { recursive: true });
  await fs.mkdir(outsideDir, { recursive: true });

  const mock = new MockApi(options.mock);
  await mock.start();

  const env = environment({
    PDF2X_API_BASE_URL: mock.url,
    PDF2X_CONFIG_DIR: configDir,
    PDF2X_ALLOWED_ROOTS: root,
    PDF2X_TRANSFER_CONCURRENCY: '2',
    PDF2X_MCP_TRANSPORT: 'stdio',
    ...(options.env ?? {}),
  });

  let current = await startClient(env);

  const harness: Harness = {
    client: current.client,
    mock,
    root,
    configDir,
    outsideDir,
    baseDir,
    env,
    call: (name, args) => current.client.callTool({ name, arguments: args }),
    async restart() {
      await current.client.close();
      current = await startClient(env);
      harness.client = current.client;
    },
    async close() {
      await current.client.close();
      await mock.stop();
    },
  };
  // Keep the transport referenced so a premature GC cannot close the pipes.
  void current.transport;
  return harness;
}

/** Writes a fixture file inside the allowed root. */
export async function writeFixture(
  directory: string,
  filename: string,
  content: Buffer | string,
): Promise<string> {
  const target = path.join(directory, filename);
  await fs.writeFile(target, content);
  return target;
}

export function sha256(content: Buffer | string): string {
  return createHash('sha256').update(content).digest('hex');
}

/** Polls `probe` until it returns a truthy value or the deadline passes. */
export async function waitFor<T>(
  probe: () => Promise<T | null | undefined | false>,
  label: string,
  timeoutMs = 20_000,
  intervalMs = 50,
): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  let lastError: unknown = null;
  while (Date.now() < deadline) {
    try {
      const value = await probe();
      if (value) return value;
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  throw new Error(
    `Timed out waiting for ${label}${lastError ? `: ${(lastError as Error).message}` : ''}`,
  );
}
