/**
 * Supervises the packaged Python backend (T12, doc 03 §4).
 *
 * - spawn without a shell, with an explicit environment (absolute dirs);
 * - ask the backend to bind a free loopback port (no "test port then assume");
 * - read the authenticated readiness line and verify boot id/version;
 * - restart only the process tree this app created, never a foreign one.
 */

import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { existsSync } from 'node:fs';
import { join } from 'node:path';

export interface RuntimeEndpoint {
  port: number;
  baseUrl: string;
  bootId: string;
  version: string;
}

export interface SupervisorOptions {
  /** Directory holding the frozen backend (resources/backend). */
  backendDir: string;
  /** Executable name inside backendDir. */
  executable?: string;
  appDataDir: string;
  workDir: string;
  logDir: string;
  webDistDir: string;
  /** Extra environment (profile-derived). */
  env?: Record<string, string>;
  /** Startup budget; surfaces a diagnostic instead of a fake window. */
  startupTimeoutMs?: number;
  /** Test seam: replaces spawn. */
  spawnImpl?: typeof spawn;
  spawnArgs?: string[];
}

export class SupervisorError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = 'SupervisorError';
    this.code = code;
  }
}

const READY_PREFIX = 'CONVERTER_READY ';

export class BackendSupervisor extends EventEmitter {
  private child: ChildProcessWithoutNullStreams | null = null;
  private endpoint: RuntimeEndpoint | null = null;
  private stdoutBuffer = '';
  private readonly options: Required<Pick<SupervisorOptions, 'startupTimeoutMs'>> & SupervisorOptions;

  constructor(options: SupervisorOptions) {
    super();
    this.options = { startupTimeoutMs: 90_000, ...options };
  }

  get running(): boolean {
    return this.child !== null && this.endpoint !== null;
  }

  get currentEndpoint(): RuntimeEndpoint | null {
    return this.endpoint;
  }

  resolveExecutable(): string {
    const name =
      this.options.executable ?? (process.platform === 'win32' ? 'converter.exe' : 'converter');
    const candidate = join(this.options.backendDir, name);
    if (!existsSync(candidate)) {
      throw new SupervisorError(
        'BACKEND_MISSING',
        `Backend empacotado não encontrado em ${candidate}.`,
      );
    }
    return candidate;
  }

  buildEnvironment(extra: Record<string, string> = {}): NodeJS.ProcessEnv {
    const bootstrap = Buffer.from(
      `${process.pid}:${Date.now()}:${Math.random().toString(36).slice(2)}`,
    ).toString('base64url');
    return {
      ...process.env,
      APP_DATA_DIR: this.options.appDataDir,
      WORK_DIR: this.options.workDir,
      LOG_DIR: this.options.logDir,
      STORAGE_LOCAL_ROOT: join(this.options.appDataDir, 'storage'),
      WEB_DIST_DIR: this.options.webDistDir,
      CONVERTER_HOST: '127.0.0.1',
      CONVERTER_PORT: '0',
      DESKTOP_BOOTSTRAP_SECRET: bootstrap,
      CONVERTER_QUEUE: 'inline',
      ...(this.options.env ?? {}),
      ...extra,
    };
  }

  /** Spawn and wait for readiness; rejects with the captured stderr tail. */
  async start(extraEnv: Record<string, string> = {}): Promise<RuntimeEndpoint> {
    if (this.running) {
      return this.endpoint as RuntimeEndpoint;
    }
    const executable = this.resolveExecutable();
    const spawnImpl = this.options.spawnImpl ?? spawn;
    const child = spawnImpl(executable, this.options.spawnArgs ?? [], {
      cwd: this.options.appDataDir,
      env: this.buildEnvironment(extraEnv),
      // Never `shell: true`: arguments must not be interpreted by a shell.
      shell: false,
      windowsHide: true,
    }) as ChildProcessWithoutNullStreams;

    this.child = child;
    this.stdoutBuffer = '';
    const stderrChunks: string[] = [];
    const endpoint = await new Promise<RuntimeEndpoint>((resolve, reject) => {
      const timer = setTimeout(() => {
        cleanup();
        reject(
          new SupervisorError(
            'STARTUP_TIMEOUT',
            `O backend não ficou pronto em ${Math.round(
              (this.options.startupTimeoutMs ?? 90_000) / 1000,
            )} s. Diagnóstico: ${stderrChunks.join('').slice(-800) || 'sem saída de erro'}.`,
          ),
        );
      }, this.options.startupTimeoutMs);

      const onData = (chunk: Buffer) => {
        this.stdoutBuffer += chunk.toString('utf8');
        let index = this.stdoutBuffer.indexOf('\n');
        while (index >= 0) {
          const line = this.stdoutBuffer.slice(0, index).trim();
          this.stdoutBuffer = this.stdoutBuffer.slice(index + 1);
          if (line.startsWith(READY_PREFIX)) {
            try {
              const payload = JSON.parse(line.slice(READY_PREFIX.length)) as {
                port: number;
                boot_id: string;
                version: string;
              };
              const resolved: RuntimeEndpoint = {
                port: Number(payload.port),
                baseUrl: `http://127.0.0.1:${Number(payload.port)}`,
                bootId: String(payload.boot_id),
                version: String(payload.version),
              };
              this.endpoint = resolved;
              cleanup();
              resolve(resolved);
              return;
            } catch {
              // Malformed readiness: keep waiting until the timeout.
            }
          }
          index = this.stdoutBuffer.indexOf('\n');
        }
      };

      const onError = (error: Error) => {
        cleanup();
        reject(new SupervisorError('BACKEND_SPAWN_FAILED', `Falha ao iniciar o backend: ${error.message}`));
      };

      const onExit = (code: number | null) => {
        cleanup();
        this.endpoint = null;
        this.child = null;
        reject(
          new SupervisorError(
            'BACKEND_EXITED',
            `O backend encerrou durante a inicialização (código ${code ?? 'null'}). ${stderrChunks
              .join('')
              .slice(-800)}`,
          ),
        );
      };

      const cleanup = () => {
        clearTimeout(timer);
        child.stdout.removeListener('data', onData);
        child.removeListener('error', onError);
        child.removeListener('exit', onExit);
      };

      child.stdout.on('data', onData);
      child.stderr.on('data', (chunk: Buffer) => {
        stderrChunks.push(chunk.toString('utf8'));
        this.emit('log', chunk.toString('utf8'));
      });
      child.on('error', onError);
      child.on('exit', onExit);
    });

    child.on('exit', (code) => {
      this.endpoint = null;
      this.child = null;
      this.emit('stopped', code);
    });
    return endpoint;
  }

  /** Graceful stop of the process tree this app created (never foreign PIDs). */
  async stop(timeoutMs = 8_000): Promise<void> {
    const child = this.child;
    if (!child) return;
    this.endpoint = null;
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        try {
          child.kill('SIGKILL');
        } catch {
          /* already gone */
        }
        resolve();
      }, timeoutMs);
      child.once('exit', () => {
        clearTimeout(timer);
        resolve();
      });
      try {
        child.kill('SIGTERM');
      } catch {
        clearTimeout(timer);
        resolve();
      }
    });
    this.child = null;
  }

  /** Restart for a profile change; waits for drain before killing (T13). */
  async restart(extraEnv: Record<string, string> = {}): Promise<RuntimeEndpoint> {
    await this.stop();
    return this.start(extraEnv);
  }
}
