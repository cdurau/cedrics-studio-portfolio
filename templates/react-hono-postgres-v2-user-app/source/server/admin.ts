import { sql } from "drizzle-orm";
import { auth } from "./auth.js";
import { db } from "./db/client.js";

// Admin CLI functions. Passwords are passed in-memory only (CLI reads them
// via hidden stdin prompt); nothing is ever logged or written to disk.

type AdminContext = Awaited<(typeof auth)["$context"]>;

async function adminContext(): Promise<AdminContext> {
  return await auth.$context;
}

export async function seedUser(
  email: string,
  name: string,
  password: string,
  opts: { verified?: boolean } = {},
) {
  const ctx = await adminContext();
  const verified = opts.verified ?? true;
  const user = await ctx.internalAdapter.createUser(
    {
      email: email.toLowerCase(),
      name,
      emailVerified: verified,
    },
    // Provisioning source (required in 1.7.5): admin-seeded user.
    { method: "admin" },
  );
  const userId = (user as { id: string }).id;
  await ctx.internalAdapter.createAccount({
    userId,
    providerId: "credential",
    accountId: userId,
    password: await ctx.password.hash(password),
  });
  return user;
}

export async function resetPassword(email: string, newPassword: string) {
  const ctx = await adminContext();
  const found = await ctx.internalAdapter.findUserByEmail(email.toLowerCase());
  const userId = (found as { user?: { id: string } } | null)?.user?.id ?? (found as { id?: string })?.id;
  if (!userId) throw new Error(`User not found: ${email}`);
  const accounts = await ctx.internalAdapter.findAccounts(userId);
  const credential = (accounts as Array<{ id: string; providerId: string }>).find(
    (a) => a.providerId === "credential",
  );
  if (!credential) throw new Error(`No credential account for: ${email}`);
  await ctx.internalAdapter.updateAccount(credential.id, {
    password: await ctx.password.hash(newPassword),
  });
}

export async function revokeSessions(email: string) {
  const ctx = await adminContext();
  const found = await ctx.internalAdapter.findUserByEmail(email.toLowerCase());
  const userId = (found as { user?: { id: string } } | null)?.user?.id ?? (found as { id?: string })?.id;
  if (!userId) throw new Error(`User not found: ${email}`);
  // Incident revoke = server-side session deletion (CONTRACTS-v2), NOT a
  // secret change. Sessions are DB-backed; deletion invalidates immediately
  // (session cookie cache is disabled).
  await db.execute(sql`DELETE FROM "session" WHERE user_id = ${userId}`);
  return true;
}