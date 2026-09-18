import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { serve } from "@hono/node-server";
import { serveStatic } from "@hono/node-server/serve-static";
import { desc, sql } from "drizzle-orm";
import { Hono } from "hono";
import { db } from "./db/client.js";
import { entries } from "./db/schema.js";
import { getAuthentikIdentity } from "./auth.js";
import { proxySecretConfigured } from "./proxySecret.js";
import { proxySecretGuard } from "./guard.js";

const port = Number.parseInt(process.env.PORT ?? "3000", 10);
const clientDir = "dist/client";

const app = new Hono();

// G2 fail-closed guard (review S2): global deny-by-default via the shared
// proxySecretGuard — see guard.ts for the contract and health exceptions.
app.use("*", proxySecretGuard);

// Boot-time cross-check (G2): in production the app refuses to start without
// a syntactically valid digest. Local dev without auth infrastructure can
// set SKIP_PROXY_SECRET_BOOT_CHECK=1 explicitly; it is ignored whenever
// NODE_ENV=production (review S2).
const skipBootCheck =
  process.env.SKIP_PROXY_SECRET_BOOT_CHECK === "1" &&
  process.env.NODE_ENV !== "production";
if (!proxySecretConfigured() && !skipBootCheck) {
  console.error(
    "fatal: STUDIO_PROXY_SECRET_SHA256 missing/malformed — refusing to start (fail-closed)",
  );
  process.exit(1);
}

// G3: Liveness only — no DB query, no sensitive information.
app.get("/healthz", (c) => c.json({ ok: true }));

// G3: Readiness — PostgreSQL-backed; 503 when not ready.
app.get("/readyz", async (c) => {
  try {
    await db.execute(sql`select 1`);
    return c.json({ ok: true });
  } catch {
    return c.json({ ok: false }, 503);
  }
});

app.get("/api/me", (c) => {
  const identity = getAuthentikIdentity(c);
  return c.json({ identity });
});

app.get("/api/entries", async (c) => {
  const rows = await db
    .select({
      id: entries.id,
      content: entries.content,
      createdAt: entries.createdAt,
    })
    .from(entries)
    .orderBy(desc(entries.id));
  return c.json({ entries: rows });
});

app.post("/api/entries", async (c) => {
  const body = await c.req.json().catch(() => null);
  const content = typeof body?.content === "string" ? body.content.trim() : "";
  if (!content) {
    return c.json({ error: "content is required" }, 400);
  }
  if (content.length > 500) {
    return c.json({ error: "content is too long" }, 400);
  }

  const [created] = await db.insert(entries).values({ content }).returning({
    id: entries.id,
    content: entries.content,
    createdAt: entries.createdAt,
  });

  return c.json({ entry: created }, 201);
});

app.use(
  "/*",
  serveStatic({
    root: clientDir,
  }),
);

app.get("/*", async (c) => {
  const html = await readFile(join(clientDir, "index.html"), "utf8");
  return c.html(html);
});

serve({ fetch: app.fetch, hostname: "0.0.0.0", port }, (info) => {
  console.log(`react-hono-postgres-v2 profile listening on ${info.address}:${info.port}`);
});