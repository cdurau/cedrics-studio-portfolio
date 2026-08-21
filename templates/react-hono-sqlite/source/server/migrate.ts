import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import type Database from "better-sqlite3";

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

function ensureJournalTable(db: Database.Database): void {
  db.exec(`
    CREATE TABLE IF NOT EXISTS __drizzle_migrations (
      id integer PRIMARY KEY AUTOINCREMENT,
      hash text NOT NULL,
      tag text,
      created_at integer
    )
  `);
  const tableInfo = db.prepare("PRAGMA table_info(__drizzle_migrations)").all() as Array<{ name: string }>;
  if (!tableInfo.find((c) => c.name === "tag")) {
    db.exec("ALTER TABLE __drizzle_migrations ADD COLUMN tag text");
  }
  db.exec(`
    CREATE UNIQUE INDEX IF NOT EXISTS __drizzle_migrations_tag_uidx
    ON __drizzle_migrations (tag)
    WHERE tag IS NOT NULL
  `);
}

function appliedTags(db: Database.Database): Map<string, string> {
  const result = db.prepare("SELECT tag, hash FROM __drizzle_migrations WHERE tag IS NOT NULL").all() as Array<{ tag: string, hash: string }>;
  return new Map(result.map((row) => [row.tag, row.hash]));
}

function applyMigration(
  db: Database.Database,
  entry: PreparedEntry,
): void {
  const transaction = db.transaction(() => {
    db.exec(entry.sqlText);
    db.prepare("INSERT INTO __drizzle_migrations (hash, tag, created_at) VALUES (?, ?, ?)").run(entry.hash, entry.tag, Date.now());
  });
  transaction();
}

export async function runMigrations(
  db: Database.Database,
  migrationsDir: string,
): Promise<MigrateResult> {
  const entries = await loadJournal(migrationsDir);
  const result: MigrateResult = {
    applied: [],
    skipped: [],
  };

  const transaction = db.transaction(() => {
    ensureJournalTable(db);
  });
  transaction();

  const done = appliedTags(db);
  for (const entry of entries) {
    if (done.has(entry.tag)) {
      if (done.get(entry.tag) !== entry.hash) {
        throw new Error(`Migration hash mismatch for ${entry.tag}. The migration file has been modified.`);
      }
      result.skipped.push(entry.tag);
      continue;
    }
    applyMigration(db, entry);
    done.set(entry.tag, entry.hash);
    result.applied.push(entry.tag);
    console.log(`applied ${entry.tag}`);
  }

  return result;
}

// Run directly if this script is executed
if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
  const { sqlite } = await import("./db/client.js");
  const migrationsDir = join(process.cwd(), "drizzle", "migrations");
  console.log(`Running migrations from ${migrationsDir}`);
  runMigrations(sqlite, migrationsDir)
    .then(() => {
      console.log("Migration complete");
      sqlite.close();
      process.exit(0);
    })
    .catch((err) => {
      console.error("Migration failed", err);
      sqlite.close();
      process.exit(1);
    });
}
