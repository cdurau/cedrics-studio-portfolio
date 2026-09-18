import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { createHash } from "node:crypto";
import { Hono } from "hono";
import { proxySecretGuard } from "../source/server/guard.js";

// Guard integration tests (review S2): the REAL middleware (source/server/
// guard.ts) is exercised via Hono's app.request() — no network listener.
// Contract: global deny-by-default, health exempt, missing uid never
// authenticates, correct secret + uid passes.

const SECRET = "unit-test-plaintext-secret-value-0123456789abcdef";
const SECRET_DIGEST = createHash("sha256").update(SECRET).digest("hex");

const prev = process.env.STUDIO_PROXY_SECRET_SHA256;
afterEach(() => {
  if (prev === undefined) {
    delete process.env.STUDIO_PROXY_SECRET_SHA256;
  } else {
    process.env.STUDIO_PROXY_SECRET_SHA256 = prev;
  }
});

describe("proxySecretGuard middleware (G2, deny-by-default)", () => {
  let app: Hono;

  beforeEach(() => {
    app = new Hono();
    app.use("*", proxySecretGuard);
    app.get("/healthz", (c) => c.json({ ok: true }));
    app.get("/readyz", (c) => c.json({ ok: true }));
    app.get("/api/me", (c) => c.json({ ok: true }));
    app.get("/auth/somewhere", (c) => c.json({ ok: true }));
  });

  it("403 without any headers", async () => {
    process.env.STUDIO_PROXY_SECRET_SHA256 = SECRET_DIGEST;
    const res = await app.request("/api/me");
    expect(res.status).toBe(403);
  });

  it("403 with secret but without uid (missing uid never authenticates)", async () => {
    process.env.STUDIO_PROXY_SECRET_SHA256 = SECRET_DIGEST;
    const res = await app.request("/api/me", {
      headers: { "X-Studio-Proxy-Secret": SECRET },
    });
    expect(res.status).toBe(403);
  });

  it("403 with uid but without or wrong secret", async () => {
    process.env.STUDIO_PROXY_SECRET_SHA256 = SECRET_DIGEST;
    const resNoSecret = await app.request("/api/me", {
      headers: { "X-authentik-uid": "abc" },
    });
    expect(resNoSecret.status).toBe(403);
    const resWrong = await app.request("/api/me", {
      headers: { "X-Studio-Proxy-Secret": "wrong", "X-authentik-uid": "abc" },
    });
    expect(resWrong.status).toBe(403);
  });

  it("200 with valid secret and uid", async () => {
    process.env.STUDIO_PROXY_SECRET_SHA256 = SECRET_DIGEST;
    const res = await app.request("/api/me", {
      headers: { "X-Studio-Proxy-Secret": SECRET, "X-authentik-uid": "abc" },
    });
    expect(res.status).toBe(200);
  });

  it("non-/api routes are also guarded (deny-by-default)", async () => {
    process.env.STUDIO_PROXY_SECRET_SHA256 = SECRET_DIGEST;
    const res = await app.request("/auth/somewhere");
    expect(res.status).toBe(403);
  });

  it("health endpoints stay outside the guard (200 even without digest env)", async () => {
    delete process.env.STUDIO_PROXY_SECRET_SHA256;
    expect((await app.request("/healthz")).status).toBe(200);
    expect((await app.request("/readyz")).status).toBe(200);
  });
});