# Multi-stage build for the public NestJS API.
# Build from the repository root so `packages/contracts` is available:
#   docker build -f apps/api/Dockerfile -t pdf2x-api .

# ------------------------------ deps ---------------------------------------
FROM node:22-alpine AS deps
WORKDIR /app
RUN apk add --no-cache openssl libc6-compat

COPY packages/contracts/package.json ./packages/contracts/package.json
COPY packages/contracts ./packages/contracts
COPY apps/api/package.json ./apps/api/package.json
COPY apps/api/package-lock.json* ./apps/api/
COPY apps/api/prisma ./apps/api/prisma

WORKDIR /app/apps/api
# DATABASE_PROVIDER=postgres swaps in the PostgreSQL schema before generating
# the client; the client is provider-specific and cannot be changed at runtime.
ARG DATABASE_PROVIDER=sqlite
RUN if [ "$DATABASE_PROVIDER" = "postgres" ]; then       cp prisma/schema.postgres.prisma prisma/schema.prisma;     fi
RUN npm install --no-audit --no-fund && npx prisma generate

# ------------------------------ build --------------------------------------
FROM node:22-alpine AS build
WORKDIR /app
RUN apk add --no-cache openssl libc6-compat

# The provider argument must be declared in *every* stage that uses it: a
# Dockerfile ARG is scoped to the stage, not global.
ARG DATABASE_PROVIDER=sqlite

COPY --from=deps /app/packages ./packages
COPY --from=deps /app/apps/api/node_modules ./apps/api/node_modules

WORKDIR /app/apps/api
COPY apps/api/tsconfig.json apps/api/tsconfig.build.json apps/api/nest-cli.json ./
COPY apps/api/src ./src
COPY apps/api/prisma ./prisma
RUN if [ "$DATABASE_PROVIDER" = "postgres" ]; then       cp prisma/schema.postgres.prisma prisma/schema.prisma;     fi
RUN npx prisma generate && npm run build     && mkdir -p /tmp/prisma-cli && cp -r node_modules/prisma /tmp/prisma-cli/prisma     && cp -r node_modules/@prisma /tmp/prisma-cli/at-prisma     && npm prune --omit=dev

# ----------------------------- runtime -------------------------------------
FROM node:22-alpine AS runtime
WORKDIR /app
RUN apk add --no-cache openssl libc6-compat \
    && addgroup -S app && adduser -S app -G app \
    && mkdir -p /app/data/storage /app/prisma \
    && chown -R app:app /app

ENV NODE_ENV=production \
    API_PORT=3000 \
    API_HOST=0.0.0.0 \
    STORAGE_LOCAL_ROOT=/app/data/storage

COPY --from=build --chown=app:app /app/apps/api/node_modules ./node_modules
COPY --from=build --chown=app:app /app/apps/api/dist ./dist
COPY --from=build --chown=app:app /app/apps/api/prisma ./prisma
COPY --from=build --chown=app:app /app/apps/api/package.json ./package.json
# Prisma CLI kept for idempotent schema push at boot (PostgreSQL deployments).
COPY --from=build --chown=app:app /tmp/prisma-cli/prisma ./node_modules/prisma
COPY --from=build --chown=app:app /tmp/prisma-cli/at-prisma ./node_modules/@prisma
COPY --chown=app:app apps/api/docker-entrypoint.sh ./docker-entrypoint.sh
RUN chmod +x ./docker-entrypoint.sh

USER app
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD wget -qO- http://127.0.0.1:3000/health || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["node", "dist/main.js"]
