/**
 * Local identity for the MCP adapter.
 *
 * The API resolves the owner from the `x-device-id` header (or a Bearer JWT).
 * The adapter owns a stable device id persisted with owner-only permissions and
 * never accepts an owner from a tool argument. The device id is a credential:
 * it is never logged, echoed in a tool result, or included in an error message.
 */

import { randomBytes } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';

import { ConfigError, type McpConfig } from './config.js';

export interface PrincipalContext {
  deviceId: string;
  apiBaseUrl: string;
}

interface IdentityFile {
  version: 1;
  device_id: string;
  created_at: string;
}

const IDENTITY_FILE = 'identity.json';
const DEVICE_ID_PATTERN = /^[A-Za-z0-9._:-]{8,128}$/;

/** Generates a new opaque device id. */
export function generateDeviceId(): string {
  return `mcp_${randomBytes(16).toString('hex')}`;
}

function identityPath(config: McpConfig): string {
  return path.join(config.configDir, IDENTITY_FILE);
}

async function readIdentity(file: string): Promise<IdentityFile | null> {
  let raw: string;
  try {
    raw = await fs.readFile(file, 'utf8');
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null;
    throw new ConfigError(`Unable to read ${file}: ${(error as Error).message}`);
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new ConfigError(`Identity file ${file} is not valid JSON. Delete it to regenerate.`);
  }
  if (
    typeof parsed !== 'object' ||
    parsed === null ||
    (parsed as IdentityFile).version !== 1 ||
    typeof (parsed as IdentityFile).device_id !== 'string' ||
    !DEVICE_ID_PATTERN.test((parsed as IdentityFile).device_id)
  ) {
    throw new ConfigError(`Identity file ${file} is malformed. Delete it to regenerate.`);
  }
  return parsed as IdentityFile;
}

async function writeIdentity(file: string, identity: IdentityFile): Promise<void> {
  const directory = path.dirname(file);
  await fs.mkdir(directory, { recursive: true, mode: 0o700 });
  const temporary = path.join(
    directory,
    `.${IDENTITY_FILE}.${process.pid}.${randomBytes(4).toString('hex')}.tmp`,
  );
  const handle = await fs.open(temporary, 'wx', 0o600);
  try {
    await handle.writeFile(`${JSON.stringify(identity, null, 2)}\n`, 'utf8');
    await handle.sync();
  } finally {
    await handle.close();
  }
  try {
    await fs.rename(temporary, file);
  } catch (error) {
    await fs.rm(temporary, { force: true });
    throw error;
  }
}

/**
 * Resolves the principal used for every API request.
 *
 * Precedence: `PDF2X_DEVICE_ID` when set, otherwise the persisted identity,
 * otherwise a freshly generated id written with mode 0o600.
 */
export async function resolvePrincipal(config: McpConfig): Promise<PrincipalContext> {
  if (config.deviceId) {
    return { deviceId: config.deviceId, apiBaseUrl: config.apiBaseUrl };
  }

  const file = identityPath(config);
  const existing = await readIdentity(file);
  if (existing) {
    return { deviceId: existing.device_id, apiBaseUrl: config.apiBaseUrl };
  }

  const identity: IdentityFile = {
    version: 1,
    device_id: generateDeviceId(),
    created_at: new Date().toISOString(),
  };
  await writeIdentity(file, identity);
  return { deviceId: identity.device_id, apiBaseUrl: config.apiBaseUrl };
}
