# Phase 10 Profile — react-hono-sqlite
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN apk add --no-cache python3 make g++ && npm ci
COPY . .
RUN npm run build

FROM node:22-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production
ENV PORT=3000
ENV DB_PATH=/data/app.db
RUN addgroup -S app && adduser -S -G app app \
  && apk add --no-cache wget python3 make g++
COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force \
  && apk del python3 make g++
# Ensure non-root runtime can write to default mounted volume
RUN mkdir /data && chown app:app /data
COPY --from=build /app/dist ./dist
COPY --from=build /app/drizzle ./drizzle
USER app
EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=5s --start-period=25s --retries=5 \
  CMD wget -qO- http://127.0.0.1:3000/healthz | grep -q '"ok":true'
CMD ["sh", "-c", "node dist/server/server/migrate.js && node dist/server/server/server.js"]
