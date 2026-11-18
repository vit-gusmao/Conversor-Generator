# Conversor Generator — Web

React 19 + TypeScript + Vite 7 frontend for Conversor Generator. It uploads PDFs
and images, tracks the pipeline live over SSE, previews the generated workbook
and exposes the full audit trail (12 tabs) down to the provenance of every cell.

The frontend never parses PDFs. It talks only to the API: `/api/v1` by default, or the
`VITE_API_BASE` value baked in at build time.
There is no account system: each browser keeps its own local session (device id +
cookie) and only sees its own files.

> **Local only.** The dev server listens on `localhost:5173` (Vite default). It
> proxies `/api` to a local API. Everything the app stores lives in this browser
> (localStorage, a cookie and IndexedDB), not on a server account. See
> [`docs/STORAGE.md`](../../docs/STORAGE.md) for the full map.

## Requirements

- Node.js 22 (recommended; same major as the Docker images)
- The contracts package at `packages/contracts` (installed via `file:../../packages/contracts`)
- A running API: `apps/api` on port 3000 (full mode), or the engine on port 8000 (standalone mode)

## Install

This app is **standalone** (not part of an npm workspace):

```bash
cd apps/web
npm install
```

## Pages and routes

Routes are defined in `src/App.tsx`. Heavy pages are loaded lazily.

| Route | Page | What it does |
| --- | --- | --- |
| `/` | Home (`home-page.tsx`) | Select PDFs or images, set conversion options and start a batch upload. |
| `/batches/:id` | Batch (`batch-page.tsx`) | Live progress of every item in a batch. Retry, cancel or seal from here. |
| `/conversions/:id` | Conversion (`conversion-page.tsx`) | Pipeline progress for one conversion, streamed over SSE. |
| `/conversions/:id/result` | Result (`result-page.tsx`) | Summary, sheet selector and workbook preview, plus the exports panel for generated artifacts. |
| `/conversions/:id/audit` | Audit (`audit-page.tsx`) | The 12 audit tabs, from overview and timeline to pages, cells and issues. |
| `/history` | History (`history-page.tsx`) | Conversions of this browser's device, with search, status filter and delete. |
| `/assistant` | Assistant (`assistant-page.tsx`) | Chat over converted documents through the engine (knowledge collections and conversations). |
| `/settings` | Settings (`settings-page.tsx`) | Reads `/settings` (feature flags, limits, retention) and shows the engine version. |
| `*` | Not found | Fallback. |

## Upload behavior

Uploads use the resumable batch API, not the legacy single-shot `POST /conversions`:

1. `POST /batches` creates a batch for the session.
2. `POST /batches/:id/items` registers each file and returns an upload session.
3. Parts are sent with `PUT /uploads/:id/parts/:n` and the `x-part-sha256` header. Each part is hashed with SHA-256 in the browser before it is sent.
4. `POST /uploads/:id/complete` assembles the file and checks the whole-file hash.
5. `POST /batches/:id/seal` closes the batch once every item is uploaded.

Up to **3 documents transfer at the same time** (`MAX_PARALLEL_TRANSFERS` in
`src/services/transfer.ts`). This is client-side politeness. Within one document, parts are sent one at a time. `UPLOAD_CONCURRENCY` is not used by the web client and is not advertised by the server.

Before upload, the client validates size against `upload.max_file_bytes` from `/settings`. Both backends return `null` by default, so the client does not check size. The standalone server still rejects batch items above `UPLOAD_MAX_BYTES` (50 MiB). The whole-file SHA-256 has no fixed 64 MiB limit; `LARGE_FILE_HASH_WARNING_BYTES` is an unused constant.

Interrupted uploads resume from the server's `next_part`, so parts already
confirmed are not sent again.

### Persistence across reloads

- **IndexedDB `pdf2x-uploads`** (`src/lib/upload-persistence.ts`) stores **metadata only**: names, sizes, options, state and upload/conversion ids. It never stores `File` or `Blob` bytes.
- After a reload, queued items that still need bytes show as `needs_reselection`. Select the same files again to continue; the upload resumes from the server's `next_part`.

## Browser storage

| Key | Where | Purpose |
| --- | --- | --- |
| `pdf2x.device_id` | localStorage | Anonymous device identity sent as `x-device-id`. |
| `pdf2x_device` | cookie (readable by scripts, `SameSite=Lax`, 1 year) | Same device id. Written by the web app so clearing one of the two stores does not create a new workspace. |
| `pdf2x.auth` | localStorage (Zustand persist) | Bearer token and user from `/auth/anonymous` (`src/stores/auth.ts`). |
| `pdf2x-uploads` | IndexedDB | Upload queue metadata (see above). |

The API also sets its own HttpOnly cookie, `pdf2x_device_id`, for headerless
requests. The web app cannot read it.

Clearing site data gives the browser a new device identity, so its history is
not visible from the old one. Conversions stay on the local server.

## Development

```bash
npm run dev
```

The dev server proxies `/api` to `http://localhost:3000` by default. To use the
standalone engine (which also serves `/api/v1`), point the proxy at port 8000:

```bash
# Windows PowerShell
$env:VITE_API_PROXY_TARGET="http://localhost:8000"; npm run dev

# bash
VITE_API_PROXY_TARGET=http://localhost:8000 npm run dev
```

Or copy `.env.example` to `.env` and edit `VITE_API_PROXY_TARGET`.

SSE streams are proxied with buffering disabled, so progress updates arrive live.

## Build

```bash
npm run build       # tsc --noEmit && vite build -> dist/
npm run preview     # serve dist/ locally
```

`dist/` is the build output. In **standalone mode** the engine serves it from
`apps/web/dist` at `/`, so after `npm run build` the whole product runs from
`http://127.0.0.1:8000`. `starter-server.cmd` builds it automatically when it is
missing.

## Test

```bash
npm test          # vitest run (jsdom + @testing-library/react)
npm run test:watch
```

Coverage of the critical logic: progress weights, API error/header handling, SSE
frame parsing and resume, upload validation, and the audit tabs.

## Environment

| Variable | Default | Description |
| --- | --- | --- |
| `VITE_API_BASE` | `/api/v1` | API base URL baked into the bundle **at build time**. Precedence: a non-empty `VITE_API_BASE` wins; otherwise `/api/v1` is used. Keep the default when the dev proxy (dev) or nginx (Docker) forwards `/api`. Use an absolute URL (for example `http://localhost:8000`) only when the API runs on another local origin. |
| `VITE_API_PROXY_TARGET` | `http://localhost:3000` | **Dev-only** proxy target for `npm run dev`. Use `http://localhost:8000` in standalone mode. Read from the environment or `.env` by `vite.config.ts`. |

## Docker

The Docker build context must be the **repository root**, because the app links
`packages/contracts`:

```bash
docker build -f apps/web/Dockerfile -t pdf2x-web .
docker run --rm -p 8080:80 -e API_UPSTREAM=http://host.docker.internal:3000 pdf2x-web
```

The image is a multi-stage build (`node:22-alpine` → `nginx:alpine`), serves the SPA
with history fallback and proxies `/api` to `API_UPSTREAM` (default `http://api:3000`)
with SSE buffering disabled. To change `VITE_API_BASE` in this image, pass the build
argument: `docker build --build-arg VITE_API_BASE=... -f apps/web/Dockerfile .`

For the full stack in containers on your machine (interface, API and engine), see
[`docs/DEPLOY.md`](../../docs/DEPLOY.md).

## Related docs

- [`docs/FLOW.md`](../../docs/FLOW.md) — end-to-end flow
- [`docs/CONFIGURATION.md`](../../docs/CONFIGURATION.md) — environment reference
- [`docs/OPERATIONS.md`](../../docs/OPERATIONS.md) — backup, cleanup and logs
