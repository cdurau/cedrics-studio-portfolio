import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { serve } from "@hono/node-server";
import { serveStatic } from "@hono/node-server/serve-static";
import { desc, sql } from "drizzle-orm";
import { Hono } from "hono";
import { db } from "./db/client.js";
import { entries, users } from "./db/schema.js";
import { requireUser, withUser } from "./guard.js";
import { auth } from "./auth.js";
import { validateRuntimeEnv } from "./env.js";

// Fail-closed: refuse to boot with missing/malformed runtime secrets
// (Terra-Review 2026-09-19 M1 — better-auth 1.7.5 would silently fall back
// to its built-in default secret).
validateRuntimeEnv();

const port = Number.parseInt(process.env.PORT ?? "3000", 10);
const clientDir = "dist/client";

const app = new Hono();

// Deny-by-default on all API routes; auth endpoints and health are exempted
// inside requireUser. The client shell (login screen) is served unauthenticated.
app.use("/api/*", requireUser);

// Better Auth API
app.on(["POST", "GET"], "/api/auth/**", (c) => {
  return auth.handler(c.req.raw);
});

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

app.get("/api/me", async (c) => {
  const userId = c.get("userId" as never) as string;
  const session = await auth.api.getSession({
    headers: c.req.raw.headers
  });
  return c.json({
    userId,
    authUserId: session?.user?.id,
    email: session?.user?.email
  });
});

app.get("/api/entries", async (c) => {
  const userId = c.get("userId" as never) as string;
  const rows = await withUser(userId, async (tx) => {
    return await tx
      .select({
        id: entries.id,
        content: entries.content,
        createdAt: entries.createdAt,
      })
      .from(entries)
      .orderBy(desc(entries.id));
  });
  return c.json({ entries: rows });
});

app.post("/api/entries", async (c) => {
  const userId = c.get("userId" as never) as string;
  const body = await c.req.json().catch(() => null);
  const content = typeof body?.content === "string" ? body.content.trim() : "";
  if (!content) {
    return c.json({ error: "content is required" }, 400);
  }
  if (content.length > 500) {
    return c.json({ error: "content is too long" }, 400);
  }

  const created = await withUser(userId, async (tx) => {
    const [row] = await tx.insert(entries).values({ content, ownerId: userId }).returning({
      id: entries.id,
      content: entries.content,
      createdAt: entries.createdAt,
    });
    return row;
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
  try {
    const html = await readFile(join(clientDir, "index.html"), "utf8");
    return c.html(html);
  } catch {
    return c.text("Not Found", 404);
  }
});

serve({ fetch: app.fetch, hostname: "0.0.0.0", port }, (info) => {
  console.log(`react-hono-postgres-v2-user-app profile listening on ${info.address}:${info.port}`);
});
