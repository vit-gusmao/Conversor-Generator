/**
 * Config unit tests: the HTTP transport must bind loopback by default and
 * only listen on another interface when explicitly asked to.
 */

import assert from 'node:assert/strict';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';

import { loadConfig } from '../src/config.js';

const baseEnv = (extra: Record<string, string> = {}): NodeJS.ProcessEnv => ({
  PDF2X_CONFIG_DIR: path.join(os.tmpdir(), 'pdf2x-config-test'),
  PDF2X_MCP_TRANSPORT: 'http',
  ...extra,
});

test('HTTP transport binds 127.0.0.1 by default', () => {
  const config = loadConfig(baseEnv());
  assert.equal(config.httpHost, '127.0.0.1');
  assert.equal(config.httpPort, 7801);
});

test('PDF2X_MCP_HOST overrides the bind address', () => {
  const config = loadConfig(baseEnv({ PDF2X_MCP_HOST: '0.0.0.0' }));
  assert.equal(config.httpHost, '0.0.0.0');
});

test('blank PDF2X_MCP_HOST falls back to the loopback default', () => {
  const config = loadConfig(baseEnv({ PDF2X_MCP_HOST: '   ' }));
  assert.equal(config.httpHost, '127.0.0.1');
});
