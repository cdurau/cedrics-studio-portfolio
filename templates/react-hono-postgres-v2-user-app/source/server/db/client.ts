import pg from "pg";
import { drizzle } from "drizzle-orm/node-postgres";

const { Pool } = pg;

// Server runtime pool. In production this MUST be the runtime DB role
// (app_runtime): not table owner, no BYPASSRLS — RLS policies on business
// tables apply to it. Never fall back to MIGRATION_DATABASE_URL here: the
// server process may see that env (Coolify runtime env) but must not run
// with the owner role. Migrations construct their own owner-role pool in
// migrate.ts.
export const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
});

export const db = drizzle(pool);