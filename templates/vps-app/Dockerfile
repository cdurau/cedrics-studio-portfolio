# VPS app — derived from the locked S0 Better-Auth baseline
# L2 (Gesamtreview 2026-09-20): auth profile builds pinned by digest (reproducible);
# refresh deliberately via the digest, never by re-floating the tag.
FROM node:22-alpine@sha256:b64da1de5a51067ab8e75f0bc8dbd0905d8894baa22261f439a4572f41291e50 AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG APP_COMMIT_SHA
RUN test -n "$APP_COMMIT_SHA" && APP_COMMIT_SHA="$APP_COMMIT_SHA" npm run build

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
COPY entrypoint.sh ./entrypoint.sh
# Source migrations can be mode 0600 on the developer host. Keep root ownership
# and grant read access so the non-root bootstrap can read its journal/SQL.
RUN chmod -R a+rX /app/dist /app/drizzle && chmod 0555 /app/entrypoint.sh
USER app
EXPOSE 3000
HEALTHCHECK --interval=10s --timeout=5s --start-period=60s --retries=5 \
  CMD wget -qO- http://127.0.0.1:3000/readyz | grep -q '"ok":true'
ENTRYPOINT ["/app/entrypoint.sh"]
