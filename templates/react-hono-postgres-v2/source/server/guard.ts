import type { Context, Next } from "hono";
import { verifyProxySecretHeader } from "./proxySecret.js";

// G2 fail-closed guard, global deny-by-default (review S2): EVERY route is
// rejected without a verified proxy secret except the explicit health
// exceptions (/healthz, /readyz), which carry no DB data and must answer
// for liveness/readiness probes. New routes are therefore covered by
// default; only the health block below is outside the guard.
//
// G2 also requires that a missing uid never authenticates: the guard
// additionally demands a non-empty X-authentik-uid (set exclusively by the
// trusted ForwardAuth chain, never by clients — scrubbed upstream).
export async function proxySecretGuard(c: Context, next: Next) {
  const path = c.req.path;
  if (path === "/healthz" || path === "/readyz") {
    await next();
    return;
  }
  const header = c.req.header("X-Studio-Proxy-Secret");
  const uid = c.req.header("X-authentik-uid");
  if (!verifyProxySecretHeader(header) || !uid) {
    return c.json({ error: "proxy secret verification failed" }, 403);
  }
  await next();
}