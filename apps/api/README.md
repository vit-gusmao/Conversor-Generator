# @pdf2x/api

Public REST API for the universal PDF → Excel platform.

Node/NestJS 11 owns **identity, ownership, persistence and streaming**. It never
parses a PDF: every conversion is delegated over HTTP to the Python engine
(`services/converter`) using an internal token.

## Requirements

- Node.js 22 (recommended; same major as the Docker images). `package.json` declares `>=20`.
- The Python engine running on `CONVERTER_INTERNAL_URL` (default `http://localhost:8000`)

## Quick start

```bash
cd apps/api
npm install
npx prisma generate
npx prisma db push          # creates prisma/data/api.db (SQLite)
npm run build
npm run start:prod          # http://localhost:3000/api/v1
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
The full list is in [`.env.example`](../../.env.example).

| Variable | Default | Meaning |
| --- | --- | --- |
| `API_PORT` | `3000` | HTTP port |
| `API_DATABASE_URL` | `file:./data/api.db` | Prisma datasource (SQLite) |
| `API_POSTGRES_URL` | — | used by `prisma/schema.postgres.prisma` |
| `JWT_SECRET` | dev value | HMAC secret for bearer tokens |
| `JWT_EXPIRES_IN` | `7d` | token lifetime (`7d`, `12h`, `3600`) |
| `AUTH_MODE` | `optional` | `optional` = device sessions, `required` = 401 without token |
| `UPLOAD_MAX_BYTES` | `52428800` | upload limit (multer + validation) |
| `UPLOAD_MAX_PAGES` | `500` | maximum estimated page count |
| `ALLOWED_ORIGINS` | `http://localhost:5173,http://localhost:3000` | CORS allow-list |
| `CONVERTER_INTERNAL_URL` | `http://localhost:8000` | Python engine base URL |
| `CONVERTER_INTERNAL_TOKEN` | dev value | sent as `x-internal-token` |
| `CONVERTER_TIMEOUT_MS` | `30000` | per-request timeout |
| `CONVERTER_POLL_INTERVAL_MS` | `800` | tracker poll interval |
| `STORAGE_BACKEND` | `local` | `local` or `s3` |
| `STORAGE_LOCAL_ROOT` | `./data/storage` | local storage root |
| `S3_*` | — | MinIO/S3 credentials (SDK loaded lazily) |
| `RETENTION_*_DAYS` | `7/30/90` | exposed through `/settings` |

## API surface

`/api/v1` is the public prefix; `GET /health` also answers without the prefix.

- `POST /auth/anonymous`, `GET /auth/me` — sessão local por navegador, sem cadastro ou senha
- `POST /conversions` (multipart `file`, optional `Idempotency-Key`) → **202**
- `GET /conversions/:id/text`, `/exports`, `/exports/:exportId/download`
- `GET /capabilities`, `GET /models/status`
- `/knowledge/collections`, `/knowledge/index-jobs`
- `/chat/conversations`, `/chat/conversations/:id/messages` (SSE)
- `GET /conversions` (`status`, `q`, `page`, `page_size`)
- `GET /conversions/:id`, `/result`, `/preview`, `/download`
- `GET /conversions/:id/events` (SSE, `Last-Event-ID` / `?last_event_id=`)
- `POST /conversions/:id/retry`, `POST /conversions/:id/cancel`, `DELETE /conversions/:id`
- `GET /conversions/:id/audit`, `/audit/events`, `/audit/pages/:page`,
  `/audit/cells`, `/audit/issues`, `/audit/export`
- `GET /settings`, `GET /health`
- `POST /internal/hooks/converter` (engine push, guarded by `x-internal-token`)

Errors are always `{ "error": { "code", "message", "details" } }` with pt-BR
messages. Status transitions follow `packages/contracts`; terminal states never
go back to active — `retry` creates a new `ConversionRun`.

### Anonymous access

In `AUTH_MODE=optional` a request without a bearer token is bound to a device
identity taken from `x-device-id` (a user row is created on first sight). The
header is echoed back with a generated value when the client does not send one,
so conversions stay private per device.

## Engine contract (client side)

`ConverterClient` calls `/internal/v1/jobs/{runId}/...`:

| Method | Route |
| --- | --- |
| `enqueue` | `POST /enqueue` |
| `status` | `GET ""` |
| `cancel` | `POST /cancel` |
| `result` | `GET /result` |
| `preview` | `GET /preview?sheet=&offset=&limit=` |
| `download` | `GET /download` |
| `audit*` | `GET /audit`, `/audit/events`, `/audit/pages/{n}`, `/audit/cells`, `/audit/issues`, `/audit/export` |
| `health` | `GET /internal/v1/health` |

Every call sends `x-internal-token`. Jobs are enqueued through a transactional
outbox: the row is committed before the HTTP call, so a crash between "task
created" and "engine notified" is retried every 5 s.

## Storage

`LocalStorageService` writes content-addressed keys (`ab/<sha256>.pdf`) and
rejects path traversal. `S3StorageService` requires `@aws-sdk/client-s3`
(installed lazily — the app boots, and tests pass, without it).

## Database

SQLite by default (`prisma/schema.prisma`). For PostgreSQL copy
`prisma/schema.postgres.prisma` over `prisma/schema.prisma`, set
`API_POSTGRES_URL` and run `npx prisma generate && npx prisma db push`.

In Docker, do not copy by hand: pass `--build-arg DATABASE_PROVIDER=postgres`
when building the image and `DATABASE_PROVIDER=postgres` at runtime. The
`docker-entrypoint.sh` then applies the schema with `prisma db push` on every
start. The entrypoint does this only for PostgreSQL. With SQLite the tables must
exist before the first request.

The repository has no versioned migrations folder (`prisma/` contains only the
schemas and `seed.ts`), so `prisma migrate deploy` has nothing to apply.

## Docker

Build from the repository root (the app depends on `packages/contracts`):

```bash
docker build -f apps/api/Dockerfile --build-arg DATABASE_PROVIDER=postgres -t pdf2x-api .
docker run --rm -p 3000:3000 --env-file .env -v pdf2x-data:/app/data pdf2x-api
```

The default image build uses SQLite. `--env-file .env` copies every value,
including `localhost` addresses, which are wrong inside a container. Set the
container addresses explicitly. See [`docs/DEPLOY.md`](../../docs/DEPLOY.md).
