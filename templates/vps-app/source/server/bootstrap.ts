import { randomUUID } from "node:crypto";
import { pathToFileURL } from "node:url";
import { join } from "node:path";
import { Pool, type PoolClient } from "pg";
import { hash } from "@node-rs/argon2";
import { runMigrations } from "./migrate.js";

export function bootstrapUrls(env = process.env) {
  try {
    const runtime = new URL(env.DATABASE_URL ?? "");
    const owner = new URL(env.MIGRATION_DATABASE_URL ?? "");
    if (![runtime, owner].every(u => ["postgres:", "postgresql:"].includes(u.protocol) && !u.search && !u.hash)
      || runtime.username !== "app_runtime" || owner.username !== "postgres"
      || runtime.host !== owner.host || runtime.pathname !== owner.pathname
      || runtime.pathname !== "/postgres" || !/^[a-f0-9]{64}$/.test(runtime.password)
      || !owner.password) throw new Error();
    return { runtime, owner };
  } catch { throw new Error("INVALID_BOOTSTRAP_URLS"); }
}

export async function bootstrap(env = process.env) {
  const { runtime, owner } = bootstrapUrls(env);
  const pool = new Pool({ connectionString: owner.href, max: 2, connectionTimeoutMillis: 5000,
    statement_timeout: 30000, query_timeout: 35000 });
  const rt = new Pool({ connectionString: runtime.href, max: 1, connectionTimeoutMillis: 5000 });
  let lock: PoolClient | undefined;
  let locked = false;
  try {
    lock = await pool.connect();
    const deadline = Date.now() + 30000;
    do {
      locked = (await lock.query("SELECT pg_try_advisory_lock(73190421) AS locked")).rows[0].locked;
      if (!locked) await new Promise(r => setTimeout(r, 200));
    } while (!locked && Date.now() < deadline);
    if (!locked) throw new Error("BOOTSTRAP_LOCK_TIMEOUT");
    const roles = await lock.query(`SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreatedb,
      rolcreaterole, rolreplication FROM pg_roles WHERE rolname = 'app_runtime'`);
    if (!roles.rowCount) {
      // Strictly validated hex from DATABASE_URL; never generate another password.
      await lock.query(`CREATE ROLE app_runtime LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD '${runtime.password}'`);
    } else {
      const r = roles.rows[0];
      const membership = await lock.query("SELECT 1 FROM pg_auth_members WHERE member = 'app_runtime'::regrole");
      const owned = await lock.query("SELECT 1 FROM pg_class WHERE relowner = 'app_runtime'::regrole UNION ALL SELECT 1 FROM pg_namespace WHERE nspowner = 'app_runtime'::regrole UNION ALL SELECT 1 FROM pg_database WHERE datdba = 'app_runtime'::regrole");
      if (!r.rolcanlogin || r.rolsuper || r.rolbypassrls || r.rolcreatedb || r.rolcreaterole || r.rolreplication || membership.rowCount || owned.rowCount)
        throw new Error("UNSAFE_RUNTIME_ROLE");
    }
    try { await rt.query("SELECT 1"); }
    catch (error) {
      if ((error as {code?: string}).code === "28P01") throw new Error("ROLE_PASSWORD_MISMATCH");
      throw new Error("RUNTIME_CONNECTION_FAILED");
    }
    await runMigrations(pool, join(process.cwd(), "drizzle/migrations"));
    await lock.query("BEGIN");
    try {
      await lock.query('LOCK TABLE "user" IN EXCLUSIVE MODE');
      if (!(await lock.query('SELECT 1 FROM "user" LIMIT 1')).rowCount) {
        const email = env.ADMIN_EMAIL?.trim().toLowerCase();
        const password = env.ADMIN_PASSWORD;
        if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) || !password || password.length < 12)
          throw new Error("INVALID_ADMIN_SEED");
        const id = randomUUID();
        const passwordHash = await hash(password, { memoryCost: 19456, timeCost: 2, outputLen: 32, parallelism: 1 });
        await lock.query('INSERT INTO "user" (id,name,email,email_verified,created_at,updated_at) VALUES ($1,$2,$3,true,now(),now())', [id, "Admin", email]);
        await lock.query('INSERT INTO account (id,account_id,provider_id,user_id,password,created_at,updated_at) VALUES ($1,$2,\'credential\',$2,$3,now(),now())', [randomUUID(), id, passwordHash]);
        await lock.query('INSERT INTO users (auth_user_id) VALUES ($1)', [id]);
      }
      await lock.query("COMMIT");
    } catch (error) { await lock.query("ROLLBACK"); throw error; }
  } finally {
    try {
      if (lock) {
        try { if (locked) await lock.query("SELECT pg_advisory_unlock(73190421)"); }
        finally { lock.release(); }
      }
    } finally { await Promise.all([rt.end(), pool.end()]); }
  }
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  try { await bootstrap(); }
  catch (error) {
    const safe = ["INVALID_BOOTSTRAP_URLS", "BOOTSTRAP_LOCK_TIMEOUT", "UNSAFE_RUNTIME_ROLE", "ROLE_PASSWORD_MISMATCH", "RUNTIME_CONNECTION_FAILED", "INVALID_ADMIN_SEED"];
    console.error(safe.includes((error as Error).message) ? (error as Error).message : "BOOTSTRAP_FAILED");
    process.exitCode = 1;
  }
}
