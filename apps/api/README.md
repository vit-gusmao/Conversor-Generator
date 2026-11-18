# @pdf2x/api

Public REST API for the universal PDF → Excel platform.

Node/NestJS 11 owns **identity, ownership, persistence and streaming**. It never
parses a PDF: every conversion is delegated over HTTP to the Python engine
(`services/converter`) using an internal token.

> **Local only.** By default the HTTP server binds `API_HOST=127.0.0.1` on
> `API_PORT=3000` (set `API_HOST=0.0.0.0` only inside a container). The engine is
> expected at `CONVERTER_INTERNAL_URL` (default `http://localhost:8000`). Data lives
> in SQLite under `apps/api/prisma/data/` and in local files under
> `apps/api/data/storage/`. See [`docs/STORAGE.md`](../../docs/STORAGE.md) for the
> full map and [`docs/CONFIGURATION.md`](../../docs/CONFIGURATION.md) for every variable.

## Requirements

- Node.js 22 (recommended; same major as the Docker images). `package.json` declares `>=20`.
- The Python engine reachable at `CONVERTER_INTERNAL_URL` (default `http://localhost:8000`).

## Quick start

```bash
cd apps/api
npm install
npx prisma generate
npx prisma db push          # creates prisma/data/api.db (SQLite)
npm run build
npm run start:prod          # http://127.0.0.1:3000/api/v1
```

Development:

```bash
npm run start:dev
npm test                    # unit tests (no database required)
npm run test:e2e            # HTTP e2e against in-memory fakes
```

Useful scripts: `npm run typecheck`. `npm run db:seed` only prints the local
session policy: the application has no accounts and creates no user.

## Configuration

Loaded with `@nestjs/config` from `../../.env` first, then `apps/api/.env`.
Real environment variables always win. Every value has a development default.
Replace `JWT_SECRET` and `CONVERTER_INTERNAL_TOKEN` before exposing the service.
The full list is in [`.env.example`](../../.env.example) and
[`docs/CONFIGURATION.md`](../../docs/CONFIGURATION.md).

### Server and identity

| Variable | Default | Meaning |
| --- | --- | --- |
| `API_PORT` | `3000` | HTTP port |
| `API_HOST` | `127.0.0.1` | Interface the server binds to. Containers set `0.0.0.0`. |
| `PUBLIC_API_URL` | `http://localhost:3000` | Informational: advertised public URL. Not used by request handling. |
| `RUNTIME_MODE` | `full` | Reported as `runtime_mode` in `/settings`. It does not change routing. |
| `ALLOWED_ORIGINS` | `http://localhost:5173,http://localhost:3000` | CORS allow-list |
| `API_DATABASE_URL` | `file:./data/api.db` | Prisma datasource (SQLite). Resolved relative to `prisma/schema.prisma`, see [Database](#database). |
| `API_POSTGRES_URL` | — | used by `prisma/schema.postgres.prisma` |
| `JWT_SECRET` | dev value | HMAC secret for bearer tokens |
| `JWT_EXPIRES_IN` | `7d` | token lifetime (`7d`, `12h`, `3600`) |
| `AUTH_MODE` | `optional` | `optional` = device sessions, `required` = 401 without token |

### Uploads and batches

| Variable | Default | Meaning |
| --- | --- | --- |
| `UPLOAD_MAX_BYTES` | `52428800` (50 MiB) | Limit of the legacy single-shot `POST /conversions` (multer + content-length check). |
| `UPLOAD_MAX_PAGES` | `null` (no ceiling) | Optional operator cap on the legacy endpoint. When set to a number, `POST /conversions` rejects documents above it with `TOO_MANY_PAGES`. `0`, `none` or `null` mean no cap. The default product flow processes documents in full. |
| `UPLOAD_PART_BYTES` | `8388608` (8 MiB) | Default part size advertised to the resumable uploader. A transport parameter, not a document limit. |
| `UPLOAD_PART_MAX_BYTES` | `33554432` (32 MiB) | Largest accepted single part. |
| `UPLOAD_CONCURRENCY` | `3` | Read by the configuration but not advertised or used; the web client uses the constant `MAX_PARALLEL_TRANSFERS = 3`. |
| `UPLOAD_MAX_FILE_BYTES` | `null` (no ceiling) | Per-file limit enforced when a batch item is registered. |
| `UPLOAD_SESSION_TTL_HOURS` | `24` | How long an incomplete upload session stays resumable. |
| `MAX_FILES_PER_BATCH` | `null` | Advertised through `/settings` only. **Not enforced** by the batch endpoints. |
| `BATCH_UPLOAD_ENABLED` | `true` | Rollout flag. `false` rejects creating new batches (`batch_disabled`); conversions already started are not interrupted. |
| `LARGE_DOCUMENT_PIPELINE_ENABLED` | `true` | Rollout flag reported as `full_document` in `/settings`. |
| `RESULT_COMPACT_ENABLED` | `true` | Rollout flag reported as `result_compact` in `/settings`. |
| `MCP_ENABLED` | `true` | Reported as `mcp` in `/settings`. It does not start or stop the MCP adapter. |

### Engine and storage

| Variable | Default | Meaning |
| --- | --- | --- |
| `CONVERTER_INTERNAL_URL` | `http://localhost:8000` | Python engine base URL |
| `CONVERTER_INTERNAL_TOKEN` | dev value | sent as `x-internal-token` in both directions (API → engine, engine → `/internal/hooks/converter`) |
| `CONVERTER_TIMEOUT_MS` | `30000` | per-request timeout |
| `CONVERTER_POLL_INTERVAL_MS` | `800` | status tracker poll interval (fallback when callbacks are missing) |
| `CONVERTER_HEALTH_CACHE_MS` | `30000` | cache TTL of the engine health probe used by `/settings` |
| `STORAGE_BACKEND` | `local` | `local` or `s3` |
| `STORAGE_LOCAL_ROOT` | `./data/storage` | Local storage root. Resolved against the **current working directory** of the process (`apps/api` when started with the scripts above). |
| `S3_*` | — | MinIO/S3 credentials (`S3_ENDPOINT`, `S3_REGION`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET`, `S3_FORCE_PATH_STYLE`, `S3_SECURE`). SDK loaded lazily. |
| `RETENTION_PDF_DAYS` / `RETENTION_XLSX_DAYS` / `RETENTION_AUDIT_DAYS` | `7` / `30` / `90` | Exposed through `/settings`. |

## API surface

`/api/v1` is the public prefix. `GET /health` also answers without the prefix
(container healthchecks). There is no `/health/ready` route.

**Auth and identity**

- `POST /auth/anonymous` — creates or reuses a local device session (no sign-up, no password). Sets the HttpOnly cookie `pdf2x_device_id`. Returns `201` in both `AUTH_MODE` values; `AUTH_MODE=required` applies to the guarded routes.
- `GET /auth/me`

**Batches and resumable uploads**

- `POST /batches`, `GET /batches/:id`, `POST /batches/:id/seal`, `POST /batches/:id/cancel`
- `POST /batches/:id/items` — registers a file and creates its upload session
- `GET /batches/:id/items`, `POST /batches/:id/items/:itemId/cancel`, `POST /batches/:id/items/:itemId/retry`
- `GET /batches/:id/events` (SSE, aggregated per batch, `Last-Event-ID` replay)
- `GET /uploads/:id`, `GET /uploads/:id/parts`, `PUT /uploads/:id/parts/:part` (raw bytes, optional `x-part-sha256` header), `POST /uploads/:id/complete`, `DELETE /uploads/:id`

**Conversions (legacy and per-conversion)**

- `POST /conversions` (multipart `file`, optional `Idempotency-Key`) → **202**. Single-shot path, subject to `UPLOAD_MAX_BYTES` and the optional `UPLOAD_MAX_PAGES`.
- `GET /conversions` (`status`, `q`, `page`, `page_size`)
- `GET /conversions/:id`, `/result`, `/result-summary`, `/sheets`, `/sheets/:sheetId/pages`, `/preview`, `/pages`, `/tables`, `/cells/:cellId`, `/download`
- `GET /conversions/:id/events` (SSE, `Last-Event-ID` / `?last_event_id=`)
- `POST /conversions/:id/retry`, `POST /conversions/:id/cancel`, `DELETE /conversions/:id`
- `GET /conversions/:id/audit`, `/audit/events`, `/audit/pages/:page`, `/audit/cells`, `/audit/issues`, `/audit/export`

**Engine proxy** (forwarded to the Python engine; see the list below)

- `GET /capabilities`, `GET /models/status`
- `GET|POST /conversions/:id/exports`, `GET /conversions/:id/exports/:exportId/download`, `GET /conversions/:id/text`
- `GET|POST /knowledge/collections`, `DELETE /knowledge/collections/:collectionId`, `POST /knowledge/index-jobs`, `GET /knowledge/index-jobs/:jobId`
- `GET|POST /chat/conversations`, `GET|DELETE /chat/conversations/:conversationId`, `POST /chat/conversations/:conversationId/messages` (the response streams as SSE)

`/knowledge/search` is **not** proxied by this API.

**Other**

- `GET /settings` — feature flags, limits and retention as seen by the client
- `GET /health` — DB, queue, storage and engine probes
- `POST /internal/hooks/converter` — engine push, guarded by `x-internal-token`. Not for browsers.

Errors are always `{ "error": { "code", "message", "details" } }` with pt-BR
messages. Status transitions follow `packages/contracts`; terminal states never
go back to active — `retry` creates a new `ConversionRun`.

### Anonymous access and identity

Every public route uses `OptionalJwtGuard`. It resolves the caller in this order:

1. **Bearer token.** `Authorization: Bearer <jwt>`, or `?access_token=<jwt>` on SSE routes (EventSource cannot send headers). A valid token selects that user.
2. **Invalid or missing token.** With `AUTH_MODE=required` the request gets 401 (`invalid_token` or `missing_token`). With `AUTH_MODE=optional` the guard continues to step 3.
3. **Device identity.** The first valid value of: the `x-device-id` header, then the `?device_id=` query parameter, then the `pdf2x_device_id` cookie. A device id maps to an anonymous user row created on first sight, so anonymous conversions stay private per device.
4. **No valid id.** A new device id is generated and echoed in the `x-device-id` response header. Clients persist it.

## Engine contract (client side)

`ConverterClient` calls `/internal/v1/jobs/{runId}/...` and `/internal/v1/health`:

| Method | Route |
| --- | --- |
| Engine operation | Route (under `/internal/v1/jobs/{runId}`) |
| --- | --- |
| enqueue | `POST /enqueue` |
| status | `GET` (job root) |
| cancel | `POST /cancel` |
| result | `GET /result` |
| result summary | `GET /result-summary` |
| sheets | `GET /sheets`, `GET /sheets/{sheetId}/pages` |
| preview | `GET /preview?sheet=&offset=&limit=` |
| pages, tables, cell | `GET /pages`, `GET /tables`, `GET /cells/{cellId}` |
| download | `GET /download` |
| audit | `GET /audit`, `/audit/events`, `/audit/pages/{n}`, `/audit/cells`, `/audit/issues`, `/audit/export` |
| engine health | `GET /internal/v1/health` (no job id) |

Every call sends `x-internal-token`. Timeouts follow `CONVERTER_TIMEOUT_MS`.

### Outbox (engine delivery)

Jobs and cancellations are written to an `OutboxEvent` table in the same
database transaction as the conversion and its run, then delivered by
`OutboxService` (`modules/conversions/outbox.service.ts`):

- The drainer runs every **5 s** and takes up to 20 rows whose `availableAt` has passed. Dispatch is also attempted right away after a create.
- A dispatcher claims a row with a compare-and-set **lease of 60 s**. A row whose lease expired is picked up again. The engine is idempotent per `run_id`, so a replay is harmless.
- A failed delivery is retried with exponential backoff (`min(60 s, 1 s × 2^attempts)`). After **8 attempts** the row becomes `failed`, and the conversion and its run are marked `failed` with **`QUEUE_DELIVERY_FAILED`**, so nothing sits in `queued` forever. `OutboxService.requeue()` can move a failed row back to `pending`; no HTTP route calls it today.

A crash between "task created" and "engine notified" is therefore recovered by the drainer.

### Status sync

The engine reports progress by two channels that share one state machine:

- **Callback** — the engine calls `POST /internal/hooks/converter` as soon as a stage changes.
- **Poll** — `ConversionTrackerService` reconciles active runs every `CONVERTER_POLL_INTERVAL_MS` (default **800 ms**). It covers missed callbacks.

Progress reaches the browser through the SSE routes listed above.

## Storage

`LocalStorageService` resolves every key against `STORAGE_LOCAL_ROOT` (relative to
the process working directory, so `apps/api/data/storage` in the default setup)
and rejects any key that escapes that root.

| Object | Key layout |
| --- | --- |
| Stored document | `documents/<ownerId>/<ab>/<sha256>.<ext>` — `<ab>` is the first two hex chars of the SHA-256; `<ext>` is the document extension (`pdf` by default, the detected format for images). Keys are derived from the content hash, so the same bytes for one owner map to the same key. |
| Upload part | `uploads/<uploadId>/<00001>.part` (sequential part number, 5 digits) |
| Upload part in flight | `uploads/<uploadId>/incoming/<00001>-<nonce>.part`, renamed into place only after a full write |
| Assembled upload | `uploads/<uploadId>/assembled.bin` before it is moved to its document key |

`S3StorageService` uses the same key layout in the bucket `S3_BUCKET`. It requires
`@aws-sdk/client-s3`, installed lazily: the app boots, and tests pass, without it.

## Database

SQLite by default (`prisma/schema.prisma`). The datasource URL
`file:./data/api.db` is resolved by Prisma **relative to the schema file**, so the
database is `apps/api/prisma/data/api.db`, not `apps/api/data/api.db`.

For PostgreSQL copy `prisma/schema.postgres.prisma` over `prisma/schema.prisma`, set
`API_POSTGRES_URL` and run `npx prisma generate && npx prisma db push`.

In Docker, do not copy by hand: pass `--build-arg DATABASE_PROVIDER=postgres`
when building the image and `DATABASE_PROVIDER=postgres` at runtime. The
`docker-entrypoint.sh` then applies the schema with `prisma db push` on every
start. The entrypoint does this only for PostgreSQL. With SQLite the tables must
exist before the first request.

The repository has no versioned Prisma migrations folder (`prisma/` contains only
the schemas and `seed.ts`), so `prisma migrate deploy` has nothing to apply. The
versioned migrations for the engine are in `services/converter/migrations`.

## Docker

Build from the repository root (the app depends on `packages/contracts`):

```bash
docker build -f apps/api/Dockerfile --build-arg DATABASE_PROVIDER=postgres -t pdf2x-api .
docker run --rm -p 3000:3000 --env-file .env -v pdf2x-data:/app/data pdf2x-api
```

The default image build uses SQLite. `--env-file .env` copies every value,
including `localhost` addresses, which are wrong inside a container. Set the
container addresses explicitly (for example `API_HOST=0.0.0.0` and
`CONVERTER_INTERNAL_URL=http://converter:8000`). See
[`docs/DEPLOY.md`](../../docs/DEPLOY.md).

## Related docs

- [`docs/FLOW.md`](../../docs/FLOW.md) — end-to-end request flow
- [`docs/STORAGE.md`](../../docs/STORAGE.md) — where each kind of data lives
- [`docs/OPERATIONS.md`](../../docs/OPERATIONS.md) — backup, cleanup and logs
- [`docs/CONFIGURATION.md`](../../docs/CONFIGURATION.md) — environment reference
