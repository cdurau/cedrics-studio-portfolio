import { pgTable, serial, text, timestamp, uuid } from "drizzle-orm/pg-core";
import { users } from "../auth-schema.js";

export const entries = pgTable("entries", {
  id: serial("id").primaryKey(),
  ownerId: uuid("owner_id").references(() => users.id).notNull(),
  content: text("content").notNull(),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
});
