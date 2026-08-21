import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import postgres from "postgres";

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

async function ensureJournalTable(sql: postgres.Sql): Promise<void> {
  await sql.unsafe(`
    CREATE TABLE IF NOT EXISTS public.__drizzle_migrations (
      id serial PRIMARY KEY,
      hash text NOT NULL,
      tag text,
      created_at bigint
    )
  `);
  await sql.unsafe(
    "ALTER TABLE public.__drizzle_migrations ADD COLUMN IF NOT EXISTS tag text"
  );
  await sql.unsafe(`
    CREATE UNIQUE INDEX IF NOT EXISTS __drizzle_migrations_tag_uidx
    ON public.__drizzle_migrations (tag)
    WHERE tag IS NOT NULL
  `);
}

async function appliedTags(sql: postgres.Sql): Promise<Map<string, string>> {
  const result = await sql<{ tag: string, hash: string }[]>`SELECT tag, hash FROM public.__drizzle_migrations WHERE tag IS NOT NULL`;
  return new Map(result.map((row) => [row.tag, row.hash]));
}

async function applyMigration(
  sql: postgres.Sql,
  entry: PreparedEntry,
): Promise<void> {
  await sql.begin(async (tx) => {
    await tx.unsafe(entry.sqlText);
    await tx`INSERT INTO public.__drizzle_migrations (hash, tag, created_at) VALUES (${entry.hash}, ${entry.tag}, ${Date.now()})`;
  });
}

export async function runMigrations(
  sql: postgres.Sql,
  migrationsDir: string,
): Promise<MigrateResult> {
  const entries = await loadJournal(migrationsDir);
  const result: MigrateResult = {
    applied: [],
    skipped: [],
  };

  await ensureJournalTable(sql);

  const done = await appliedTags(sql);
  for (const entry of entries) {
    if (done.has(entry.tag)) {
      if (done.get(entry.tag) !== entry.hash) {
        throw new Error(`Migration hash mismatch for ${entry.tag}. The migration file has been modified.`);
      }
      result.skipped.push(entry.tag);
      continue;
    }
    await applyMigration(sql, entry);
    done.set(entry.tag, entry.hash);
    result.applied.push(entry.tag);
    console.log(`applied ${entry.tag}`);
  }

  return result;
}

// Run directly if this script is executed
const isMain = process.argv[1] && (
  process.argv[1].endsWith('migrate.ts') ||
  process.argv[1].endsWith('migrate.js')
);

if (isMain) {

  const connectionString = process.env.DATABASE_URL;
  if (!connectionString) {
    console.error("DATABASE_URL is not set");
    process.exit(1);
  }
  const sql = postgres(connectionString, { max: 1 });
  const migrationsDir = join(process.cwd(), "drizzle");
  console.log(`Running migrations from ${migrationsDir}`);
  runMigrations(sql, migrationsDir)
    .then(async (res) => {
      console.log("Migration complete", res);
      await sql.end();
      process.exit(0);
    })
    .catch(async (err) => {
      console.error("Migration failed", err);
      await sql.end();
      process.exit(1);
    });
}
