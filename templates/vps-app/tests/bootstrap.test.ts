import { describe, it, expect, afterAll } from "vitest";
import { Pool } from "pg";
import { bootstrap } from "../source/server/bootstrap.js";
const pool = new Pool({ connectionString: process.env.TEST_DATABASE_URL });
const env = { DATABASE_URL: process.env.TEST_RUNTIME_DATABASE_URL,
  MIGRATION_DATABASE_URL: process.env.TEST_DATABASE_URL,
  ADMIN_EMAIL: "bootstrap@test.com", ADMIN_PASSWORD: "bootstrap-test-password" };
afterAll(async () => { await pool.end(); });
describe("real database bootstrap", () => {
  it("serializes rolling starts and seeds admin once", async () => {
    await Promise.all([bootstrap(env), bootstrap(env)]);
    expect((await pool.query('SELECT email_verified FROM "user"')).rows).toEqual([{email_verified:true}]);
    expect((await pool.query('SELECT * FROM users')).rowCount).toBe(1);
    const rows = (await pool.query("SELECT password FROM account")).rows;
    expect(rows).toHaveLength(1);
    expect(rows[0].password).toMatch(/^\$argon2id\$/);
    await bootstrap({ ...env, ADMIN_PASSWORD: "different-password-do-not-apply" });
    expect((await pool.query("SELECT password FROM account")).rows).toEqual(rows);
  }, 60000);
  it("mismatched password fails without rotation", async () => {
    const changed = new URL(env.DATABASE_URL!); changed.password = "b".repeat(64);
    await expect(bootstrap({ ...env, DATABASE_URL: changed.href })).rejects.toThrow("ROLE_PASSWORD_MISMATCH");
    await bootstrap(env);
  });
  it("refuses elevated existing role", async () => {
    await pool.query("ALTER ROLE app_runtime BYPASSRLS");
    try { await expect(bootstrap(env)).rejects.toThrow("UNSAFE_RUNTIME_ROLE"); }
    finally { await pool.query("ALTER ROLE app_runtime NOBYPASSRLS"); }
  });
  it("refuses runtime database ownership", async () => {
    await pool.query("ALTER DATABASE postgres OWNER TO app_runtime");
    try { await expect(bootstrap(env)).rejects.toThrow("UNSAFE_RUNTIME_ROLE"); }
    finally { await pool.query("ALTER DATABASE postgres OWNER TO postgres"); }
  });
  it("recovers from seed failure without partial user", async () => {
    await pool.query('TRUNCATE "user", account, session, users, entries CASCADE');
    await expect(bootstrap({ ...env, ADMIN_PASSWORD: "" })).rejects.toThrow("INVALID_ADMIN_SEED");
    expect((await pool.query('SELECT 1 FROM "user"')).rowCount).toBe(0);
    await bootstrap(env);
    await pool.query('TRUNCATE "user", account, session, users, entries CASCADE');
  });
});
