import { describe, it, expect, beforeAll, afterAll } from "vitest";
import Database from "better-sqlite3";
import { runMigrations, sha256 } from "../source/server/migrate";
import { join } from "node:path";
import { mkdir, writeFile, rm } from "node:fs/promises";

let db: Database.Database;
const tempMigrationsDir = join(process.cwd(), "drizzle", "temp_migrations");
const dbPath = join(process.cwd(), "test_integration.db");

describe("Database Migration Integration", () => {
  beforeAll(async () => {
    db = new Database(dbPath);
    await mkdir(join(tempMigrationsDir, "meta"), { recursive: true });
  });

  afterAll(async () => {
    db.close();
    await rm(tempMigrationsDir, { recursive: true, force: true });
    await rm(dbPath, { force: true });
  });

  it("fails if journal is missing", async () => {
    await expect(runMigrations(db, join(process.cwd(), "nonexistent"))).rejects.toThrow("Missing migration journal");
  });

  it("applies migrations cleanly", async () => {
    const sqlText = "CREATE TABLE test_table (id int);";
    await writeFile(join(tempMigrationsDir, "0000_test.sql"), sqlText);
    await writeFile(join(tempMigrationsDir, "meta", "_journal.json"), JSON.stringify({
      entries: [{ idx: 0, tag: "0000_test", breakpoints: true }]
    }));

    const result = await runMigrations(db, tempMigrationsDir);
    expect(result.applied).toContain("0000_test");
    expect(result.skipped).toHaveLength(0);
  });

  it("is idempotent (skips already applied)", async () => {
    const result = await runMigrations(db, tempMigrationsDir);
    expect(result.applied).toHaveLength(0);
    expect(result.skipped).toContain("0000_test");
  });

  it("fails if hash changes for applied migration", async () => {
    const modifiedSql = "CREATE TABLE test_table (id int, val text);";
    await writeFile(join(tempMigrationsDir, "0000_test.sql"), modifiedSql);

    await expect(runMigrations(db, tempMigrationsDir)).rejects.toThrow(/hash mismatch/);
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

    await expect(runMigrations(db, tempMigrationsDir)).rejects.toThrow(/syntax error/);

    // Check that broken_table doesn't exist
    const res = db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='broken_table'").get();
    expect(res).toBeUndefined();
  });
});
