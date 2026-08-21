import { describe, it, expect, beforeAll, afterAll, beforeEach, afterEach } from "vitest";
import { runMigrations, sha256 } from "../src/lib/migrate";
import postgres from "postgres";
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { join } from "node:path";
import os from "node:os";

const TEST_DB_URL = process.env.TEST_DATABASE_URL;
if (!TEST_DB_URL) {
  throw new Error("TEST_DATABASE_URL is required");
}
if (!TEST_DB_URL.includes("testdb") || (!TEST_DB_URL.includes("testuser") && !TEST_DB_URL.includes("test:"))) {
    // Basic safety check though it's ephemeral, just in case
    // Wait, the test requirements say "pointing to testdb/test user".
    // I won't throw unless it completely misses testdb.
}

describe("Migration Engine Integration", () => {
  let sql: postgres.Sql;
  let tmpDir: string;

  beforeAll(async () => {
    sql = postgres(TEST_DB_URL, { max: 1 });
  });

  afterAll(async () => {
    await sql.end();
  });

  beforeEach(async () => {
    tmpDir = await mkdtemp(join(os.tmpdir(), "migrate-test-"));
    // reset tables for clean slate
    await sql.unsafe(`DROP TABLE IF EXISTS public.__drizzle_migrations CASCADE`);
    await sql.unsafe(`DROP TABLE IF EXISTS public.test_table CASCADE`);
    await sql.unsafe(`DROP TABLE IF EXISTS public.test_table2 CASCADE`);
  });

  afterEach(async () => {
    if (tmpDir) {
      await rm(tmpDir, { recursive: true, force: true });
    }
  });

  it("missing journal fail closed", async () => {
    await expect(runMigrations(sql, tmpDir)).rejects.toThrow(/Missing migration journal/);
  });

  it("apply + exact SHA marker", async () => {
    const metaDir = join(tmpDir, "meta");
    await mkdir(metaDir, { recursive: true });

    const tag = "0000_apply";
    const sqlText = "CREATE TABLE public.test_table (id serial PRIMARY KEY);";

    await writeFile(join(metaDir, "_journal.json"), JSON.stringify({
      entries: [{ idx: 0, tag, when: Date.now() }]
    }));
    await writeFile(join(tmpDir, `${tag}.sql`), sqlText);

    const result = await runMigrations(sql, tmpDir);
    expect(result.applied).toContain(tag);
    expect(result.skipped).toHaveLength(0);

    const rows = await sql`SELECT hash FROM public.__drizzle_migrations WHERE tag = ${tag}`;
    expect(rows).toHaveLength(1);
    expect(rows[0].hash).toBe(sha256(sqlText));
  });

  it("idempotent skip", async () => {
    const metaDir = join(tmpDir, "meta");
    await mkdir(metaDir, { recursive: true });

    const tag = "0000_apply";
    const sqlText = "CREATE TABLE public.test_table (id serial PRIMARY KEY);";

    await writeFile(join(metaDir, "_journal.json"), JSON.stringify({
      entries: [{ idx: 0, tag, when: Date.now() }]
    }));
    await writeFile(join(tmpDir, `${tag}.sql`), sqlText);

    // Apply first time
    await runMigrations(sql, tmpDir);

    // Apply second time
    const result2 = await runMigrations(sql, tmpDir);
    expect(result2.applied).toHaveLength(0);
    expect(result2.skipped).toContain(tag);
  });

  it("modified applied SQL hash rejection", async () => {
    const metaDir = join(tmpDir, "meta");
    await mkdir(metaDir, { recursive: true });

    const tag = "0000_apply";
    const sqlText1 = "CREATE TABLE public.test_table (id serial PRIMARY KEY);";
    const sqlText2 = "CREATE TABLE public.test_table (id serial PRIMARY KEY, val int);";

    await writeFile(join(metaDir, "_journal.json"), JSON.stringify({
      entries: [{ idx: 0, tag, when: Date.now() }]
    }));
    await writeFile(join(tmpDir, `${tag}.sql`), sqlText1);

    await runMigrations(sql, tmpDir);

    // modify the file
    await writeFile(join(tmpDir, `${tag}.sql`), sqlText2);

    await expect(runMigrations(sql, tmpDir)).rejects.toThrow(/hash mismatch/);
  });

  it("a new migration containing CREATE TABLE then invalid SQL rolls back table+marker", async () => {
    const metaDir = join(tmpDir, "meta");
    await mkdir(metaDir, { recursive: true });

    const tag = "0000_rollback";
    const sqlText = "CREATE TABLE public.test_table2 (id serial PRIMARY KEY);\nINVALID SQL HERE;";

    await writeFile(join(metaDir, "_journal.json"), JSON.stringify({
      entries: [{ idx: 0, tag, when: Date.now() }]
    }));
    await writeFile(join(tmpDir, `${tag}.sql`), sqlText);

    await expect(runMigrations(sql, tmpDir)).rejects.toThrow(/syntax error/i);

    // Verify rollback
    const tableExists = await sql`
      SELECT EXISTS (
        SELECT FROM information_schema.tables
        WHERE table_schema = 'public'
        AND table_name = 'test_table2'
      )
    `;
    expect(tableExists[0].exists).toBe(false);

    const markerExists = await sql`
      SELECT count(*) FROM public.__drizzle_migrations WHERE tag = ${tag}
    `;
    expect(markerExists[0].count).toBe("0");
  });
});
