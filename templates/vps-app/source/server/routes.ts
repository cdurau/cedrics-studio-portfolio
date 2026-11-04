import { Hono } from "hono";
import { desc } from "drizzle-orm";
import { entries } from "./db/schema.js";
import { withUser } from "./guard.js";

export const routes = new Hono();
routes.get("/api/entries", async (c) => {
  const userId = c.get("userId" as never) as string;
  const rows = await withUser(userId, async (tx) => {
    return await tx
      .select({
        id: entries.id,
        content: entries.content,
        createdAt: entries.createdAt,
      })
      .from(entries)
      .orderBy(desc(entries.id));
  });
  return c.json({ entries: rows });
});

routes.post("/api/entries", async (c) => {
  const userId = c.get("userId" as never) as string;
  const body = await c.req.json().catch(() => null);
  const content = typeof body?.content === "string" ? body.content.trim() : "";
  if (!content) {
    return c.json({ error: "content is required" }, 400);
  }
  if (content.length > 500) {
    return c.json({ error: "content is too long" }, 400);
  }

  const created = await withUser(userId, async (tx) => {
    const [row] = await tx.insert(entries).values({ content, ownerId: userId }).returning({
      id: entries.id,
      content: entries.content,
      createdAt: entries.createdAt,
    });
    return row;
  });

  return c.json({ entry: created }, 201);
});
