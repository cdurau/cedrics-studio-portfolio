# ---------- Build ----------
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json ./
COPY scripts ./scripts
COPY src ./src
RUN npm run build

# ---------- Runtime (nur eine gebündelte Datei, keine node_modules) ----------
FROM node:22-alpine
ENV NODE_ENV=production \
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=3333
WORKDIR /app
COPY --from=build /app/dist/obsidian-vault-mcp.mjs ./obsidian-vault-mcp.mjs
USER node
EXPOSE 3333
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -qO- "http://127.0.0.1:${MCP_PORT}/healthz" >/dev/null || exit 1
ENTRYPOINT ["node", "/app/obsidian-vault-mcp.mjs"]
