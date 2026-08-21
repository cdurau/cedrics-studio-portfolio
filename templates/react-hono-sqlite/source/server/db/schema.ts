import { sqliteTable, integer, text } from "drizzle-orm/sqlite-core";
import { sql } from "drizzle-orm";

export const entries = sqliteTable("entries", {
  id: integer("id").primaryKey(),
  content: text("content").notNull(),
  createdAt: integer("created_at", { mode: "timestamp_ms" })
    .notNull()
    .default(sql`(cast((julianday('now') - 2440587.5)*86400000 as integer))`),
});
