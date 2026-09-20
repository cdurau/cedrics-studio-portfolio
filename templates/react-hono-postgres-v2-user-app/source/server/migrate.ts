import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import type { Pool, PoolClient } from "pg";

export type Journal = {
  entries: Array<{ idx: number; tag: string }>;
};

export type MigrateResult = {
  applied: string[];
  skipped: string[];
};

type PreparedEntry = {
  tag: string;
  sqlText: string;
  hash: string;
};

export function sha256(content: string): string {
  return createHash("sha256").update(content, "utf8").digest("hex");
}

async function loadJournal(migrationsDir: string): Promise<PreparedEntry[]> {
  const journalContent = await readFile(join(migrationsDir, "meta", "_journal.json"), "utf8").catch(() => {
    throw new Error(`Missing migration journal. Migrations must be generated and committed.`);
  });
  const journal = JSON.parse(journalContent) as Journal;

  const entries: PreparedEntry[] = [];
  for (const entry of journal.entries) {
    const sqlText = await readFile(
      join(migrationsDir, `${entry.tag}.sql`),
      "utf8"
    );
    entries.push({
      tag: entry.tag,
      sqlText,
      hash: sha256(sqlText),
    });
  }
  return entries;
}

async function ensureJournalTable(client: PoolClient): Promise<void> {
  await client.query(`
    CREATE TABLE IF NOT EXISTS public.__drizzle_migrations (
      id serial PRIMARY KEY,
      hash text NOT NULL,
      tag text,
      created_at bigint
    )
  `);
  await client.query(
    "ALTER TABLE public.__drizzle_migrations ADD COLUMN IF NOT EXISTS tag text"
  );
  await client.query(`
    CREATE UNIQUE INDEX IF NOT EXISTS __drizzle_migrations_tag_uidx
    ON public.__drizzle_migrations (tag)
    WHERE tag IS NOT NULL
  `);
}

async function appliedTags(client: PoolClient): Promise<Map<string, string>> {
  const result = await client.query<{ tag: string, hash: string }>(
    "SELECT tag, hash FROM public.__drizzle_migrations WHERE tag IS NOT NULL"
  );
  return new Map(result.rows.map((row) => [row.tag, row.hash]));
}

async function applyMigration(
  client: PoolClient,
  entry: PreparedEntry,
): Promise<void> {
  try {
    await client.query("BEGIN");
    await client.query(entry.sqlText);
    await client.query(
      "INSERT INTO public.__drizzle_migrations (hash, tag, created_at) VALUES ($1, $2, $3)",
      [entry.hash, entry.tag, Date.now()]
    );
    await client.query("COMMIT");
  } catch (error) {
    try {
      await client.query("ROLLBACK");
    } catch {
      // The original error is the one callers must see.
    }
    throw error;
  }
}

export async function runMigrations(
  pool: Pool,
  migrationsDir: string,
): Promise<MigrateResult> {
  const entries = await loadJournal(migrationsDir);
  const result: MigrateResult = {
    applied: [],
    skipped: [],
  };

  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    try {
      await ensureJournalTable(client);
      await client.query("COMMIT");
    } catch (error) {
      try {
        await client.query("ROLLBACK");
      } catch {
        // Keep the original bootstrap error.
      }
      throw error;
    }

    const done = await appliedTags(client);
    for (const entry of entries) {
      if (done.has(entry.tag)) {
        if (done.get(entry.tag) !== entry.hash) {
          throw new Error(`Migration hash mismatch for ${entry.tag}. The migration file has been modified.`);
        }
        result.skipped.push(entry.tag);
        continue;
      }
      await applyMigration(client, entry);
      done.set(entry.tag, entry.hash);
      result.applied.push(entry.tag);
      console.log(`applied ${entry.tag}`);
    }
  } finally {
    client.release();
  }

  return result;
}

// Run directly if this script is executed — use the OWNER/migration role
// (MIGRATION_DATABASE_URL), never the server's runtime pool (client.ts).
// M5 (Gesamtreview 2026-09-20): when run as a separate deploy step (the server
// CMD no longer chains migrate.js), transient DB unavailability must not fail
// the deploy outright — bounded retry with backoff, then fail closed.
if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
  const { Pool } = await import("pg");
  // MIGRATION_DATABASE_URL is mandatory (claude review 2026-09-19, MEDIUM-5):
  // the previous `|| process.env.DATABASE_URL` fallback silently ran
  // migrations with the runtime role when the owner-role URL was missing
  // from the Coolify env — the exact reverse of the role split db/client.ts
  // forbids. Fail closed instead.
  const migrationUrl = process.env.MIGRATION_DATABASE_URL;
  if (!migrationUrl) {
    console.error("Migration failed: MIGRATION_DATABASE_URL is required (owner/migration role; never fall back to the runtime DATABASE_URL)");
    process.exit(1);
  }
  const migrationPool = new Pool({ connectionString: migrationUrl });
  const migrationsDir = join(process.cwd(), "drizzle", "migrations");
  // Bounded retry for transient DB unavailability (S0-7): 5 attempts,
  // 2s * attempt backoff. Connection/terminations errors retry; SQL logic
  // errors still fail immediately (retrying them only masks real breakage).
  const MAX_ATTEMPTS = 5;
  let lastError: unknown;
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    try {
      console.log(`Running migrations from ${migrationsDir} (attempt ${attempt}/${MAX_ATTEMPTS})`);
      const res = await runMigrations(migrationPool, migrationsDir);
      console.log("Migration complete", res);
      await migrationPool.end();
      process.exit(0);
    } catch (err: any) {
      lastError = err;
      const transient =
        err?.code === "ECONNREFUSED" ||
        err?.code === "ETIMEDOUT" ||
        err?.code === "ECONNRESET" ||
        err?.code === "ENOTFOUND" ||
        err?.code === "57P01" || // admin_shutdown / terminated backend
        err?.code === "53300" || // too_many_connections
        err?.code === "08006" || // connection_failure
        /terminat|server closed|connection/i.test(String(err?.message ?? ""));
      if (!transient || attempt === MAX_ATTEMPTS) break;
      const delay = 2000 * attempt;
      console.error(`Transient migration failure (attempt ${attempt}), retrying in ${delay}ms:`, String(err?.code ?? ""), String(err?.message ?? err));
      await new Promise((r) => setTimeout(r, delay));
    }
  }
  console.error("Migration failed", lastError);
  await migrationPool.end();
  process.exit(1);
}
