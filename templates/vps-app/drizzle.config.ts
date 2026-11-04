import { defineConfig } from "drizzle-kit";

export default defineConfig({
  schema: ["./source/server/db/schema.ts", "./source/server/auth-schema.ts"],
  out: "./drizzle/migrations",
  dialect: "postgresql",
});
