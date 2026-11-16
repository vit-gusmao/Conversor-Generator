#!/bin/sh
# Container entrypoint for the public API.
#
# In a PostgreSQL deployment the schema must be pushed before the app starts.
# `prisma db push` is idempotent and safe to run on every boot; for regulated
# environments replace it with `prisma migrate deploy` and a versioned
# migrations folder.
set -eu

if [ "${DATABASE_PROVIDER:-sqlite}" = "postgres" ]; then
  echo "[entrypoint] applying PostgreSQL schema"
  npx --no-install prisma db push --skip-generate || {
    echo "[entrypoint] prisma db push failed" >&2
    exit 1
  }
fi

exec "$@"
