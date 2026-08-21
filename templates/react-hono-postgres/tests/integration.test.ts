import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { Pool } from "pg";
import { runMigrations, sha256 } from "../source/server/migrate";
import { join } from "node:path";
import { mkdir, writeFile, rm } from "node:fs/promises";

const DATABASE_URL = process.env.TEST_DATABASE_URL;
if (!DATABASE_URL || !DATABASE_URL.includes("testuser") || !DATABASE_URL.includes("testdb")) {
  throw new Error("Refusing to run integration tests: TEST_DATABASE_URL is not set to a recognized disposable test DB.");
}

let pool: Pool;
const tempMigrationsDir = join(process.cwd(), "drizzle", "temp_migrations");

describe("Database Migration Integration", () => {
  beforeAll(async () => {
    pool = new Pool({ connectionString: DATABASE_URL });
    await mkdir(join(tempMigrationsDir, "meta"), { recursive: true });
    // The ephemeral database is completely fresh, so we don't need dangerous DROP statements.
  });

  afterAll(async () => {
    await rm(tempMigrationsDir, { recursive: true, force: true });
    await pool.end();
  });

  it("fails if journal is missing", async () => {
    await expect(runMigrations(pool, join(process.cwd(), "nonexistent"))).rejects.toThrow("Missing migration journal");
  });

  it("applies migrations cleanly", async () => {
    const sqlText = "CREATE TABLE test_table (id int);";
    await writeFile(join(tempMigrationsDir, "0000_test.sql"), sqlText);
    await writeFile(join(tempMigrationsDir, "meta", "_journal.json"), JSON.stringify({
      entries: [{ idx: 0, tag: "0000_test", breakpoints: true }]
    }));

    const result = await runMigrations(pool, tempMigrationsDir);
    expect(result.applied).toContain("0000_test");
    expect(result.skipped).toHaveLength(0);
  });

  it("is idempotent (skips already applied)", async () => {
    const result = await runMigrations(pool, tempMigrationsDir);
    expect(result.applied).toHaveLength(0);
    expect(result.skipped).toContain("0000_test");
  });

  it("fails if hash changes for applied migration", async () => {
    const modifiedSql = "CREATE TABLE test_table (id int, val text);";
    await writeFile(join(tempMigrationsDir, "0000_test.sql"), modifiedSql);

    await expect(runMigrations(pool, tempMigrationsDir)).rejects.toThrow(/hash mismatch/);
  });

  it("rolls back transaction on failure", async () => {
    const originalSql = "CREATE TABLE test_table (id int);";
    await writeFile(join(tempMigrationsDir, "0000_test.sql"), originalSql);
    const brokenSql = "CREATE TABLE broken_table (id int); INVALID SYNTAX;";
    await writeFile(join(tempMigrationsDir, "0001_broken.sql"), brokenSql);
    await writeFile(join(tempMigrationsDir, "meta", "_journal.json"), JSON.stringify({
      entries: [
        { idx: 0, tag: "0000_test", breakpoints: true },
        { idx: 1, tag: "0001_broken", breakpoints: true }
      ]
    }));

    await expect(runMigrations(pool, tempMigrationsDir)).rejects.toThrow(/syntax error/);

    // Check that broken_table doesn't exist
    const res = await pool.query("SELECT to_regclass('public.broken_table');");
    expect(res.rows[0].to_regclass).toBeNull();
  });
});
