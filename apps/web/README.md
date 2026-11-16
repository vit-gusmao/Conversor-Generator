# Conversor Generator — Web

React 19 + TypeScript + Vite 7 frontend for Conversor Generator. It uploads PDFs
and images, tracks the pipeline live over SSE, previews the generated workbook
and exposes the full audit trail (12 tabs) down to the provenance of every cell.

The frontend never parses PDFs. It talks only to the public API at `VITE_API_BASE`.
There is no account system: each browser keeps its own local session (device id +
cookie) and only sees its own files.

## Requirements

- Node.js 22 (recommended; same major as the Docker images)
- The contracts package at `packages/contracts` (installed via `file:../../packages/contracts`)
- A running API (`apps/api` on port 3000, or the standalone FastAPI on port 8000)

## Install

This app is **standalone** (not part of an npm workspace):

```bash
cd apps/web
npm install
```

## Development

```bash
npm run dev
```

The dev server proxies `/api` to `http://localhost:3000` by default. To use the
standalone FastAPI (which also serves `/api/v1`), point the proxy at port 8000:

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
npm run build     # tsc --noEmit && vite build -> dist/
npm run preview   # serve dist/ locally
```

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
| `VITE_API_BASE` | `/api/v1` | Base URL baked into the bundle **at build time**. Keep it relative when the proxy (dev) or nginx (Docker) forwards `/api`. Use an absolute `https://` URL when the frontend is hosted apart from the API, for example on Vercel. |
| `VITE_API_PROXY_TARGET` | `http://localhost:3000` | Dev-only proxy target. Use `http://localhost:8000` in standalone mode. |

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

## Vercel

The frontend can be hosted on Vercel as a static SPA. The repository root contains a
`vercel.json` that defines the install command, build command, output directory
(`apps/web/dist`) and a rewrite to `index.html` for every path except `/api/`.

Settings for the Vercel project:

- **Root Directory**: the repository root (not `apps/web`), because the build needs
  `packages/contracts`.
- **Framework Preset**: Other. `vercel.json` provides the commands.
- **Environment variable** (Production, and Preview if needed):
  `VITE_API_BASE=https://<your-api-host>/api/v1`.
- After changing `VITE_API_BASE`, redeploy. The value is compiled into the bundle.

The API must allow the Vercel origin in `ALLOWED_ORIGINS` (exact match, with
`https://`, without a trailing slash). Full procedure in
[`docs/DEPLOY.md`](../../docs/DEPLOY.md).
