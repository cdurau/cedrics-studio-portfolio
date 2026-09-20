# Phase 10 Profile — react-hono-postgres-v2-user-app (S0 Better-Auth baseline)
# L2 (Gesamtreview 2026-09-20): auth profile builds pinned by digest (reproducible);
# refresh deliberately via the digest, never by re-floating the tag.
FROM node:22-alpine@sha256:b64da1de5a51067ab8e75f0bc8dbd0905d8894baa22261f439a4572f41291e50 AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:22-alpine@sha256:b64da1de5a51067ab8e75f0bc8dbd0905d8894baa22261f439a4572f41291e50 AS runtime
WORKDIR /app
ENV NODE_ENV=production
ENV PORT=3000
RUN addgroup -S app && adduser -S -G app app \
  && apk add --no-cache wget
COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force
COPY --from=build /app/dist ./dist
COPY --from=build /app/drizzle ./drizzle
USER app
EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=5s --start-period=25s --retries=5 \
  CMD wget -qO- http://127.0.0.1:3000/readyz | grep -q '"ok":true'
# M5 (Gesamtreview 2026-09-20): migrate runs as its own step with the owner
# role env (MIGRATION_DATABASE_URL) — the server process never carries it.
# Deploy pipelines run `docker run --rm --env-file <migrate-env> <image> \
#   node dist/server/server/migrate.js` BEFORE starting the app; the app CMD
# below is server-only (no migrate &&), so DB unavailability at boot cannot
# take MIGRATION_DATABASE_URL into the runtime env and migrate's fail-closed
# exit cannot restart-loop the container (S0-7).
CMD ["node", "dist/server/server/server.js"]
