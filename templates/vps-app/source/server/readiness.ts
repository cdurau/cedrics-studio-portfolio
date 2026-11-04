import { readFileSync } from "node:fs";
import { sql } from "drizzle-orm";
import { db } from "./db/client.js";

// Read once from the image, never from mutable runtime environment.
let commit = "";
try { commit = JSON.parse(readFileSync(new URL("./build-metadata.json", import.meta.url), "utf8")).commit; }
catch { /* Missing metadata fails production readiness. */ }
export async function readiness() {
  const valid = typeof commit === "string" && /^[a-f0-9]{40}$/.test(commit);
  try {
    await db.execute(sql`select 1`);
    return { ok: valid || ["development", "test"].includes(process.env.NODE_ENV ?? ""), commit: valid ? commit : null };
  } catch { return { ok: false, commit: valid ? commit : null }; }
}
