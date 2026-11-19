# @pdf2x/mcp

Model Context Protocol adapter for **Conversor Generator**. It lets an LLM host
(Claude Desktop, Cursor, a custom agent…) submit PDFs and images to the public
`/api/v1` API, follow the batch, and download the generated XLSX/TXT/CSV
artifacts — without touching the web UI.

The adapter is a thin, local, single-user client:

- the owner identity comes only from a local credential (`x-device-id`), never
  from a tool argument;
- submissions are written to a durable local ledger **before** any byte is
  uploaded, so a timed-out tool call cannot create a second batch;
- uploads are resumable part by part, and downloads are streamed to disk under
  an explicit allow-list of directories.

> **Local only.** The default transport is stdio: no port is opened. The optional
> HTTP transport binds `PDF2X_MCP_HOST=127.0.0.1` on `PDF2X_MCP_PORT=7801`. The
> adapter talks only to the API in `PDF2X_API_BASE_URL`, and it keeps its
> identity and ledger in `~/.pdf2x/` (`PDF2X_CONFIG_DIR`). Nothing is sent to a
> hosted service. See [Where data lives](#where-data-lives) and
> [`docs/STORAGE.md`](../../docs/STORAGE.md).

## Requirements

- Node.js **22+** (`engines` do `package.json`).
- A reachable Conversor Generator API. In `standalone` mode the converter
  serves `/api/v1` itself (`http://localhost:8000` by default); in `full` mode
  use the NestJS public API.
- MCP SDK: `@modelcontextprotocol/server` **2.4.0** (exact pin) and, for tests,
  `@modelcontextprotocol/client` **2.4.0**. The installed SDK negotiates
  protocol revision **2025-11-25** (`LATEST_PROTOCOL_VERSION`); structured tool
  results (`structuredContent`) are used, so hosts older than 2025-06-18 still
  receive the text summary.

## Install, build and test

From the repository root:

```bash
npm --prefix apps/mcp install
npm --prefix apps/mcp run build
npm --prefix apps/mcp test
```

The same commands are exposed at the root as `npm run install:mcp`,
`npm run build:mcp` and `npm run test:mcp`. The test suite starts an in-process
mock of the batch/upload API and drives the built `dist/main.js` through the
official MCP **client** over stdio (real negotiation, real `tools/call`). It
covers batching, idempotent replays, path policy, a wrong part hash, non
overwriting downloads and restart recovery through `next_part`.

Run the server with:

```bash
npm --prefix apps/mcp start
```

## Tools

| Tool | Arguments | Result |
| --- | --- | --- |
| `submit_files` | `request_key`, `files[]`, `batch_id?`, `auto_seal?` | `{submission_id, batch_id, status, accepted_count, rejected_count, next_cursor}` |
| `get_submission` | `submission_id`, `cursor?`, `limit?` | Per-item transfer state from the local ledger |
| `get_batch` | `batch_id` | Server counts, page progress and lifecycle status |
| `list_batch_items` | `batch_id`, `cursor?`, `limit?`, `status?` | Paged items with conversion status |
| `seal_batch` | `batch_id` | Closes the batch; `BATCH_HAS_PENDING_UPLOADS` is reported, not swallowed |
| `cancel_batch` | `batch_id` | Cancels the batch and active items |
| `cancel_item` | `batch_id`, `item_id` | Cancels one item |
| `retry_item` | `batch_id`, `item_id`, `scope?` (`upload` default, `conversion`) | Resumes an upload session from `next_part` or re-runs a conversion |
| `get_conversion_result` | `conversion_id`, `sheet?`, `offset?`, `limit?` | Compact counters, sheets, artifact metadata and a small preview (default 20 rows, max 100) |
| `download_artifact` | `conversion_id`, `export_id`, `destination?` | `{path, bytes, sha256}`; never overwrites |

Reads default to 20 items per page and are hard-capped at 100.

A file entry is:

```json
{
  "client_item_id": "invoice-2026-01",
  "source": { "type": "local_path", "path": "C:\\docs\\invoice.pdf" },
  "options": { "target_formats": ["xlsx"] }
}
```

`source` is either `local_path` (a file the adapter reads from an allowed root)
or `upload_id` (an existing upload session of the same batch). `upload_id`
sources require `batch_id` and a `client_item_id` that already exists in that
batch; they never create a second conversion — the existing ids are returned.

`submit_files` returns as soon as the submission is persisted. Poll
`get_submission` (local, always answers) or `get_batch` (server state) to follow
progress. When `auto_seal` is true the batch is sealed only after every item of
that submission finished uploading; the API still decides whether other uploads
are pending, and a 409 is surfaced as a tool error.

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `PDF2X_API_BASE_URL` | `http://localhost:8000` | API root. `.../api/v1` is accepted and normalized. The default targets the standalone engine (`services/converter`, which serves `/api/v1` itself). For full mode (NestJS API) use `http://localhost:3000`. |
| `PDF2X_DEVICE_ID` | generated | Fixed local identity. Must be 8–128 chars of `A-Z a-z 0-9 . _ : -`. |
| `PDF2X_CONFIG_DIR` | `~/.pdf2x` | Holds `identity.json` and `submissions.jsonl`. |
| `PDF2X_ALLOWED_ROOTS` | the user's home | Comma-separated absolute directories allowed for reads and writes. |
| `PDF2X_TRANSFER_CONCURRENCY` | `3` | Global part-upload concurrency across all items (1–32). |
| `PDF2X_MCP_TRANSPORT` | `stdio` | `stdio` or `http` (Streamable HTTP at `/mcp`, health at `/health`). |
| `PDF2X_MCP_PORT` | `7801` | HTTP port when the transport is `http`. |
| `PDF2X_MCP_HOST` | `127.0.0.1` | Interface the HTTP transport binds to. Loopback by default; set it explicitly only behind a trusted reverse proxy. Ignored with `stdio`. |

Invalid values abort startup with a readable `[pdf2x-mcp] configuration error: …`
message on stderr and exit code 2.

## Host configuration

### Claude Desktop / generic stdio

```json
{
  "mcpServers": {
    "pdf2x": {
      "command": "node",
      "args": ["<repo>/apps/mcp/dist/main.js"],
      "env": {
        "PDF2X_API_BASE_URL": "http://localhost:8000",
        "PDF2X_ALLOWED_ROOTS": "<pasta-permitida>"
      }
    }
  }
}
```

Replace `<repo>` with the absolute path of your clone (for example
`C:/path/to/Conversor Generator`). Build first with `npm --prefix apps/mcp run build`
so that `dist/main.js` exists.

Any host that speaks MCP over stdio can use the same `command`/`args`/`env`
triple. stdout is reserved for protocol frames: every log line is written to
stderr, so nothing corrupts the JSON-RPC stream.

### HTTP (optional)

```bash
PDF2X_MCP_TRANSPORT=http PDF2X_MCP_PORT=7801 node apps/mcp/dist/main.js
```

The server listens on `PDF2X_MCP_HOST:PDF2X_MCP_PORT`, which defaults to
`127.0.0.1:7801`. The endpoint is `POST/GET /mcp` (Streamable HTTP) and `GET /health`
returns `{"status":"ok","transport":"http","name":"pdf2x-mcp"}`. There is no authentication on this port, so do not
bind it to a public interface. Without a `destination`, `download_artifact` returns a
short-lived API reference (`{remote: true, download_url, expires_at}`) instead of
writing on the server filesystem.

## Identity and pairing with the browser

On first run the adapter generates `mcp_<32 hex>` and stores it in
`${PDF2X_CONFIG_DIR}/identity.json` with mode `0600`. That id is sent as
`x-device-id` on every request and is never logged, echoed or included in a tool
result.

The server treats a device id as an anonymous owner. To make MCP conversions
appear in the **same history** as the web UI, use the browser's id:

1. Open the Conversor Generator web app in the target browser.
2. In DevTools, read `localStorage.getItem('pdf2x.device_id')`. The web app
   also writes the same value to a readable cookie named `pdf2x_device`. The
   server sets its own HttpOnly cookie named `pdf2x_device_id`, which scripts
   cannot read, so use the localStorage value.
3. Set `PDF2X_DEVICE_ID` to that value in the MCP host configuration and restart
   the host.

The reverse also works: set the browser's `pdf2x.device_id` localStorage entry to
the value from `identity.json` only if you understand that this shares the whole
history with the adapter.

## Allowed roots and path policy

`PDF2X_ALLOWED_ROOTS` is the complete list of directories the adapter may touch.
Every path is processed by `src/path-policy.ts`, which:

- rejects `..` segments, UNC paths (`\\server\share`), device namespaces
  (`\\.\`, `\\?\`) and drive-relative paths;
- resolves the real path with `fs.realpath`, so a symlink or junction cannot
  escape a root;
- accepts only regular files (directories, sockets, FIFOs and device nodes are
  rejected);
- resolves destination directories and proves they are inside a root before
  writing.

`download_artifact` never overwrites: an existing `report.csv` becomes
`report (2).csv`, then `(3)`, and so on. Files are written to a temporary name in
the destination directory, fsynced, hash-checked, and only then renamed. With no
`destination`, downloads go to `<first allowed root>/pdf2x-downloads` (created
with mode `0700`).

The adapter never shells out and never ingests arbitrary URLs in this version.
Extracted document content is treated as data: it cannot select tools, paths or
commands.

## Recovery after a restart

`submissions.jsonl` is an append-only ledger: every state transition appends a
full snapshot and fsyncs before returning. On startup the adapter:

1. rebuilds the in-memory state from the ledger (a single truncated trailing
   line from a crash is tolerated; other corruption fails loudly);
2. re-validates every unfinished local source (existence and size);
3. resumes unfinished submissions in the background.

Resuming recomputes the whole-file SHA-256 and re-registers the item with the
same `client_item_id`. The server returns the same upload session and the
authoritative `next_part`, so already-confirmed parts are never resent. If the
file's size or hash changed, the item fails with `SOURCE_CHANGED` and its
partial upload is aborted — parts from two different documents are never mixed.

Run **one adapter process per config directory**: the ledger is durable and
fsynced, but it is not a multi-writer database, so two processes sharing
`submissions.jsonl` would race.

## Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| `configuration error: PDF2X_API_BASE_URL …` | The value is not http(s) or contains a path other than `/api/v1`. |
| `NETWORK_ERROR … Unable to reach the API` | The API is not running, or the host points at the wrong base URL. |
| `PATH_OUTSIDE_ROOTS` | The path (or its symlink target) is outside `PDF2X_ALLOWED_ROOTS`. |
| `SOURCE_NOT_FOUND` / `NOT_A_REGULAR_FILE` | The file was moved/deleted, or a directory was passed. |
| `SOURCE_CHANGED` | The file changed between the first attempt and the resume; submit it again with a new `request_key`. |
| `IDEMPOTENT_REPLAY` | `request_key` was reused with different arguments. Use a new key for a different submission. |
| `BATCH_NOT_OPEN` | The batch was sealed; create a new one (omit `batch_id`). |
| `BATCH_HAS_PENDING_UPLOADS` | Another upload in the batch is still in flight; wait, retry, or cancel that item. |
| `UPLOAD_CHECKSUM_MISMATCH` | A part or the assembled document did not match its hash. The upload was aborted; retry the item. |
| `NOT_READY` | The conversion is still running, or the result/artifact is not published yet. |
| Nothing in the web UI | The device id differs. See “Identity and pairing”. |
| Tools missing in the host | Run the built `dist/main.js` (not `src/main.ts`), and check stderr for startup errors. |

## Revocation and cleanup

- **Revoke this adapter's access locally:** delete
  `${PDF2X_CONFIG_DIR}/identity.json` (and `submissions.jsonl` if you also want
  to forget the local history). The next start creates a new identity. The old
  owner's data still exists on the server; delete those conversions through the
  API/web UI if required.
- **Stop using a paired browser identity:** remove `PDF2X_DEVICE_ID` from the
  host configuration and delete `identity.json`.
- **Rotate a leaked device id:** the API has no separate token to revoke — the id
  *is* the credential. Replace it as above and remove the old conversions.
- **Protect the config directory:** it contains the credential and a record of
  local file paths and hashes. Do not commit, sync to a shared drive, or include
  it in backups you do not control.

## Security model

- **Identity:** only the local credential selects the owner. Tool arguments
  carry no owner, user or account field.
- **Least privilege on disk:** reads and writes are confined to
  `PDF2X_ALLOWED_ROOTS`; path traversal, UNC and device paths are rejected;
  symlinks are resolved before the containment check.
- **Untrusted documents:** document bytes are hashed and uploaded, never parsed
  for instructions. Filenames are sanitized before they become download targets.
- **No accidental duplication:** `request_key` fingerprints the canonical
  arguments; replays return the same submission. The submission is persisted
  before uploading, so a timeout cannot fork the work.
- **No overwrite:** downloads choose a fresh name and verify size and SHA-256
  before the final rename.
- **Transport:** HTTP transport binds `PDF2X_MCP_HOST` (default `127.0.0.1`) on
  `PDF2X_MCP_PORT` and is intended for localhost or a trusted reverse proxy. It adds no authentication of its own;
  the adapter still authenticates to the API with the local device id.
- **Credential hygiene:** the device id is never logged. Error messages and tool
  results contain API codes/messages, not the credential.

## Where data lives

| Data | Location (default) | Notes |
| --- | --- | --- |
| Identity | `~/.pdf2x/identity.json` (`PDF2X_CONFIG_DIR`) | Mode `0600`. Contains the `mcp_…` device id. |
| Submission ledger | `~/.pdf2x/submissions.jsonl` | Append-only, fsynced. Holds local source paths, hashes and batch ids. |
| Downloads | `<first allowed root>/pdf2x-downloads` | Created with mode `0700` when no `destination` is given. |
| Server-side files | Managed by the API, not by the adapter | See [`docs/STORAGE.md`](../../docs/STORAGE.md). |

## Development notes

- `src/config.ts` — validated environment.
- `src/identity.ts` — credential resolution and persistence.
- `src/api-client.ts` — `/api/v1` client; retries only network errors, 429 and
  503 with exponential backoff and a per-operation idempotency key.
- `src/path-policy.ts` — the filesystem gate.
- `src/submission-store.ts` — append-only ledger + canonical request hash.
- `src/transfer-worker.ts` — background, resumable part transfer.
- `src/tools/*.ts` — one module per tool.
- `tests/` — mock API + official MCP client over stdio.

Wire types in `src/types.ts` mirror `packages/contracts`; if the contract
changes, update both and run `npm --prefix apps/mcp test`.
