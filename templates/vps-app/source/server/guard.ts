import type { Context, Next } from "hono";
import { sql } from "drizzle-orm";
import { auth } from "./auth.js";
import { db } from "./db/client.js";
import { users } from "./auth-schema.js";
import { eq } from "drizzle-orm";

export async function requireUser(c: Context, next: Next) {
  const path = c.req.path;
  if (path === "/healthz" || path === "/readyz" || path === "/api/auth" || path.startsWith("/api/auth/")) {
    await next();
    return;
  }

  let appUser: typeof users.$inferSelect | undefined;
  try {
    const session = await auth.api.getSession({ headers: c.req.raw.headers });
    if (!session?.user) return c.json({ error: "Unauthorized" }, 401);
    const userRows = await db.select().from(users).where(eq(users.authUserId, session.user.id));
    appUser = userRows[0];
  } catch {
    // Session/mapping DB failures must not fall through to Hono's default 500.
    // Authentication could not be established: reveal no build metadata.
    c.header("Cache-Control", "no-store");
    if (path === "/api/deployment") return c.json({ ok: false, commit: null }, 503);
    return c.json({ error: "Service unavailable" }, 503);
  }

  if (!appUser) {
    return c.json({ error: "User mapping not found" }, 401);
  }

  c.set("userId", appUser.id);
  await next();
}

export async function withUser<T>(userId: string, fn: (tx: any) => Promise<T>): Promise<T> {
  return await db.transaction(async (tx) => {
    // Parameterized set_config: userId is a uuid resolved from the session,
    // but the runtime string must never be interpolated into SQL text.
    await tx.execute(sql`SELECT set_config('app.user_id', ${userId}, true)`);
    return await fn(tx);
  });
}
