import { readiness } from "./readiness.js";
import { routes } from "./routes.js";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { serve } from "@hono/node-server";
import { serveStatic } from "@hono/node-server/serve-static";
import { Hono } from "hono";
import { requireUser } from "./guard.js";
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

// Internal health and authenticated release proof share exactly one probe.
app.get("/readyz", async (c) => {
  const result = await readiness();
  c.header("Cache-Control", "no-store");
  return c.json(result, result.ok ? 200 : 503);
});
app.get("/api/deployment", async (c) => {
  const result = await readiness();
  c.header("Cache-Control", "no-store");
  return c.json(result, result.ok ? 200 : 503);
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

app.route("/", routes);

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
  console.log(`vps-app listening on ${info.address}:${info.port}`);
});
