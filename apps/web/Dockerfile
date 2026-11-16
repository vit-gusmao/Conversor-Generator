# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Build
# Context: repository root (needed for the linked @pdf2x/contracts package)
#   docker build -f apps/web/Dockerfile -t pdf2x-web .
#
# Only the specific sources are copied (never host node_modules), so no root
# .dockerignore is required.
# ---------------------------------------------------------------------------
FROM node:22-alpine AS build

WORKDIR /repo

# Linked contracts package (its entrypoint is src/index.ts).
COPY packages/contracts/package.json ./packages/contracts/package.json
COPY packages/contracts/src ./packages/contracts/src

COPY apps/web/package.json apps/web/package-lock.json* ./apps/web/
WORKDIR /repo/apps/web
RUN npm install --no-audit --fund=false

COPY apps/web/index.html apps/web/tsconfig.json apps/web/vite.config.ts apps/web/vitest.config.ts ./apps/web/
COPY apps/web/src ./apps/web/src

ARG VITE_API_BASE=/api/v1
ENV VITE_API_BASE=${VITE_API_BASE}

RUN npm run build

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM nginx:alpine AS runtime

COPY --from=build /repo/apps/web/dist /usr/share/nginx/html
COPY apps/web/nginx.conf.template /etc/nginx/templates/default.conf.template

# Expanded by the nginx entrypoint (envsubst).
ENV API_UPSTREAM=http://api:3000

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD wget -qO- http://127.0.0.1/ >/dev/null 2>&1 || exit 1

CMD ["nginx", "-g", "daemon off;"]
