import Database from "better-sqlite3";
import { drizzle } from "drizzle-orm/better-sqlite3";
import * as schema from "./schema.js";

const dbPath = process.env.DB_PATH || "local.db";
export const sqlite = new Database(dbPath, { timeout: 5000 });

// Ensure concurrent reads and strict foreign keys are enabled per SQLite Contract
sqlite.pragma("journal_mode = WAL");
sqlite.pragma("foreign_keys = ON");
sqlite.pragma("busy_timeout = 5000");

export const db = drizzle(sqlite, { schema });
