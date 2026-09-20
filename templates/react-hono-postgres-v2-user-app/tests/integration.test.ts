import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { Pool } from "pg";
import { join } from "node:path";
import { execSync, spawn, type ChildProcess } from "node:child_process";
import { createConnection } from "node:net";
import { startMockOidc, type MockOidc } from "./mock-oidc";

// Integration suite: CONTRACTS-v2 user-app auth contract, all 12 mandatory
// tests, against an ephemeral Postgres (tests/run-integration.sh).
// The app server runs with the RUNTIME role (RLS applies); migrations and
// admin ops run with the OWNER role — mirroring the production topology.

const OWNER_URL = process.env.TEST_DATABASE_URL;
const RUNTIME_URL = process.env.TEST_RUNTIME_DATABASE_URL;
if (!OWNER_URL || !RUNTIME_URL) throw new Error("TEST_DATABASE_URL / TEST_RUNTIME_DATABASE_URL not set");

const PORT = 34567;
const baseUrl = `http://127.0.0.1:${PORT}`;

const SECRET_V1 = "s012-test-secret-one-0123456789abcdef0123456789abcdef";
const SECRET_V2 = "s012-test-secret-two-0123456789abcdef0123456789abcdef";

// Env must be set BEFORE importing any server module (client.ts builds its
// pool at import; auth.ts reads TEST_OIDC_ISSUER / BETTER_AUTH_SECRETS).
process.env.DATABASE_URL = OWNER_URL;
process.env.BETTER_AUTH_SECRETS = `1:${SECRET_V1}`;
process.env.BETTER_AUTH_URL = baseUrl;

// Mock OIDC provider (google simulation) must exist before the auth module
// is imported — the genericOAuth plugin reads TEST_OIDC_ISSUER at init.
const mock: MockOidc = await startMockOidc({
  g1: { sub: "g1", email: "u1@test.com", email_verified: true, name: "U One" },
  gdiff: { sub: "gdiff", email: "other@test.com", email_verified: true, name: "Other" },
  gunv: { sub: "gunv", email: "unverified-google@test.com", email_verified: false, name: "Unv" },
});
process.env.TEST_OIDC_ISSUER = mock.issuer;

const { runMigrations } = await import("../source/server/migrate.js");
const { seedUser, revokeSessions } = await import("../source/server/admin.js");

let ownerPool: Pool;
let serverProcess: ChildProcess | null = null;
let currentSecrets = `1:${SECRET_V1}`;

// Unique client IP per request family: the rate limiter keys on ip|path.
let ipCounter = 0;
function ip(): string {
  ipCounter += 1;
  return `10.77.${Math.floor(ipCounter / 250)}.${ipCounter % 250}`;
}
const hdr = (extra: Record<string, string> = {}) => ({
  "content-type": "application/json",
  "x-forwarded-for": ip(),
  ...extra,
});

async function waitForHealth(timeoutMs = 20000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${baseUrl}/healthz`);
      if (res.ok) return;
    } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 150));
  }
  throw new Error("server did not become healthy");
}

function startServer(): ChildProcess {
  const child = spawn("npx", ["tsx", "source/server/server.ts"], {
    cwd: join(import.meta.dirname, ".."),
    env: {
      ...process.env,
      PORT: String(PORT),
      DATABASE_URL: RUNTIME_URL,
      BETTER_AUTH_SECRETS: currentSecrets,
      BETTER_AUTH_URL: baseUrl,
      TEST_OIDC_ISSUER: mock.issuer,
    },
    stdio: ["ignore", "ignore", "pipe"],
    // Own process group (gesamtreview 2026-09-20, M2): `npx` does not reliably
    // forward SIGTERM to tsx/node. Killing only the npx pid left the old server
    // listening on PORT (Linux/npm 10.x), so the restart tests (8, 10, 12) ran
    // against the OLD process. stopServer() now signals the whole group.
    detached: true,
  });
  child.stderr?.on("data", (d) => {
    if (process.env.DEBUG_SERVER) process.stdout.write(`[srv] ${d}`);
  });
  return child;
}

function portIsFree(): Promise<boolean> {
  return new Promise((resolve) => {
    const sock = createConnection({ host: "127.0.0.1", port: PORT });
    sock.once("connect", () => { sock.destroy(); resolve(false); });
    sock.once("error", () => resolve(true));
  });
}

async function stopServer(): Promise<void> {
  if (!serverProcess) return;
  const pid = serverProcess.pid!;
  serverProcess = null;
  const signalGroup = (sig: NodeJS.Signals) => {
    try { process.kill(-pid, sig); } catch { /* group already gone */ }
  };
  signalGroup("SIGTERM");
  // A restart test is only meaningful if the old server is really gone:
  // wait until nothing listens on PORT, escalate to SIGKILL, else fail loudly.
  const deadline = Date.now() + 10000;
  let killed = false;
  while (!(await portIsFree())) {
    if (Date.now() > deadline) throw new Error(`old server still listening on ${PORT} after SIGTERM/SIGKILL`);
    if (!killed && Date.now() > deadline - 5000) { signalGroup("SIGKILL"); killed = true; }
    await new Promise((r) => setTimeout(r, 100));
  }
}

// --- helpers -----------------------------------------------------------

async function passwordSignIn(email: string, password: string): Promise<{ status: number; cookie: string | null }> {
  const res = await fetch(`${baseUrl}/api/auth/sign-in/email`, {
    method: "POST",
    headers: hdr(),
    body: JSON.stringify({ email, password }),
  });
  return { status: res.status, cookie: res.headers.get("set-cookie") };
}

function sessionCookie(setCookie: string | null): string | null {
  if (!setCookie) return null;
  const parts = setCookie.split(/,(?=[^;,]*=)/); // split cookies, not Expires dates
  for (const part of parts) {
    const token = part.split(";").map((s) => s.trim()).find((s) => s.startsWith("user-app.session_token="));
    if (token) return token;
  }
  return null;
}

function cookieJar(res: Response, existing: string | null): string {
  const set = res.headers.get("set-cookie");
  const bits: string[] = [];
  if (existing) bits.push(existing);
  if (set) for (const part of set.split(/,(?=[^;,]*=)/)) {
    const first = part.split(";")[0].trim();
    if (first && !bits.includes(first)) bits.push(first);
  }
  return bits.join("; ");
}

/** Drive one Google (mock-OIDC) flow like a browser: auth endpoint -> authorize -> callback. */
async function googleFlow(
  cookie: string | null,
  endpoint: "sign-in/social" | "link-social",
  opts: { loginHint?: string } = {},
): Promise<Response> {
  const body = JSON.stringify({
    provider: "google",
    callbackURL: "/",
    ...(opts.loginHint ? { loginHint: opts.loginHint } : {}),
  });
  // 1) auth endpoint issues the authorize URL (302) + state cookie
  const r1 = await fetch(`${baseUrl}/api/auth/${endpoint}`, {
    method: "POST",
    headers: hdr(cookie ? { cookie } : {}),
    body,
    redirect: "manual",
  });
  const authorizeUrl = r1.headers.get("location");
  if (!authorizeUrl) {
    const t = await r1.text();
    throw new Error(`no authorize redirect from ${endpoint}: ${r1.status} ${t.slice(0, 300)}`);
  }
  const jar = cookieJar(r1, cookie);
  // 2) authorize at the provider (auto-approves) -> provider callback URL
  const r2 = await fetch(authorizeUrl, { redirect: "manual", headers: { "x-forwarded-for": ip(), cookie: jar } });
  const callbackUrl = r2.headers.get("location");
  if (!callbackUrl) throw new Error("authorize did not redirect back");
  // 3) provider callback (state cookie must be present)
  return await fetch(callbackUrl, { redirect: "manual", headers: { "x-forwarded-for": ip(), cookie: jar } });
}

type Snapshot = {
  user: Array<{ id: string; email: string; email_verified: boolean }>;
  account: Array<{ provider_id: string; account_id: string; user_id: string }>;
  users: Array<{ id: string; auth_user_id: string }>;
};

async function snapshotAuthDb(): Promise<Snapshot> {
  const u = await ownerPool.query(`SELECT id, email, email_verified FROM "user" ORDER BY email`);
  const a = await ownerPool.query(`SELECT provider_id, account_id, user_id FROM "account" ORDER BY user_id, provider_id, account_id`);
  const m = await ownerPool.query(`SELECT id, auth_user_id FROM "users" ORDER BY auth_user_id`);
  return { user: u.rows, account: a.rows, users: m.rows };
}

function usersIdFor(s: Snapshot, authUserId: string): string | undefined {
  return s.users.find((r) => r.auth_user_id === authUserId)?.id;
}

function authIdFor(s: Snapshot, email: string): string {
  const row = s.user.find((u) => u.email === email);
  if (!row) throw new Error(`no auth user for ${email}`);
  return row.id;
}

// --- tests ---------------------------------------------------------------

describe("user-app Auth Contract (CONTRACTS-v2 mandatory tests)", () => {
  beforeAll(async () => {
    ownerPool = new Pool({ connectionString: OWNER_URL });
    await runMigrations(ownerPool, join(import.meta.dirname, "../drizzle/migrations"));
    serverProcess = startServer();
    await waitForHealth();
  }, 60000);

  afterAll(async () => {
    await stopServer();
    await mock.close();
    await ownerPool.end();
  });

  it("(0) better-auth exactly 1.7.5 pin asserted", () => {
    const output = execSync("npm ls better-auth --json", { cwd: join(import.meta.dirname, "..") }).toString();
    expect(JSON.parse(output).dependencies["better-auth"].version).toBe("1.7.5");
  });

  it("1+2) seeded verified login OK; unverified fixture rejected", async () => {
    await seedUser("u1@test.com", "U One", "correct-horse-battery-staple");
    await seedUser("u2@test.com", "U Two", "correct-horse-battery-staple", { verified: false });

    const ok = await passwordSignIn("u1@test.com", "correct-horse-battery-staple");
    expect(ok.status).toBe(200);
    expect(sessionCookie(ok.cookie)).toBeTruthy();

    const unv = await passwordSignIn("u2@test.com", "correct-horse-battery-staple");
    expect(unv.status).toBe(403);
    expect(sessionCookie(unv.cookie)).toBeNull();
  });

  it("3) same-email Google sign-in before linking fails (account_not_linked), DB snapshot identical", async () => {
    const before = await snapshotAuthDb();
    const res = await googleFlow(null, "sign-in/social");
    // refusal path: link-account returns "account not linked" -> callback
    // redirects to the error URL with the machine-readable code
    const loc = res.headers.get("location") ?? "";
    expect(loc).toContain("error=account_not_linked");
    expect(sessionCookie(res.headers.get("set-cookie"))).toBeNull();
    const after = await snapshotAuthDb();
    expect(after).toEqual(before);
  });

  it("4) explicit linking while authenticated -> password and Google login resolve to same users.id", async () => {
    const login = await passwordSignIn("u1@test.com", "correct-horse-battery-staple");
    const cookie = sessionCookie(login.cookie);
    expect(cookie).toBeTruthy();

    const before = await snapshotAuthDb();
    const linkRes = await googleFlow(cookie!, "link-social");
    expect(linkRes.status).toBeLessThan(400);
    const after = await snapshotAuthDb();

    // password credential preserved: same credential account rows
    const credBefore = before.account.filter((a) => a.provider_id === "credential");
    const credAfter = after.account.filter((a) => a.provider_id === "credential");
    expect(credAfter).toEqual(credBefore);
    // google account now linked to u1's auth user
    const u1AuthId = authIdFor(before, "u1@test.com");
    expect(after.account.some((a) => a.provider_id === "google" && a.user_id === u1AuthId)).toBe(true);
    const uid = usersIdFor(after, u1AuthId);
    expect(uid).toBeDefined();

    // both logins resolve to the same users.id via /api/me
    const pwMe = await (await fetch(`${baseUrl}/api/me`, { headers: hdr({ cookie: cookie! }) })).json();
    expect(pwMe.userId).toBe(uid);

    const gRes = await googleFlow(null, "sign-in/social");
    const gCookie = sessionCookie(gRes.headers.get("set-cookie"));
    expect(gCookie).toBeTruthy();
    const gMe = await (await fetch(`${baseUrl}/api/me`, { headers: hdr({ cookie: gCookie! }) })).json();
    expect(gMe.userId).toBe(uid);
  });

  it("5) different-email Google account is not merged", async () => {
    const before = await snapshotAuthDb();
    const u1AuthId = authIdFor(before, "u1@test.com");
    const res = await googleFlow(null, "sign-in/social", { loginHint: "gdiff" });
    const after = await snapshotAuthDb();
    // u1's mapping and accounts unchanged (no merge into u1)
    expect(usersIdFor(after, u1AuthId)).toBe(usersIdFor(before, u1AuthId));
    expect(after.account.filter((a) => a.user_id === u1AuthId).length)
      .toBe(before.account.filter((a) => a.user_id === u1AuthId).length);
    // the other-email google identity never attached to u1
    expect(after.account.some((a) => a.account_id === "gdiff" && a.user_id === u1AuthId)).toBe(false);
    // no session for the different-email flow (implicit sign-up disabled)
    expect(sessionCookie(res.headers.get("set-cookie"))).toBeNull();
  });

  it("6) A/B isolation at API level AND direct SQL with runtime role", async () => {
    await seedUser("ub@test.com", "U B", "correct-horse-battery-staple");
    const loginA = await passwordSignIn("u1@test.com", "correct-horse-battery-staple");
    const cookieA = sessionCookie(loginA.cookie)!;
    const loginB = await passwordSignIn("ub@test.com", "correct-horse-battery-staple");
    const cookieB = sessionCookie(loginB.cookie)!;

    // A creates an entry
    const create = await fetch(`${baseUrl}/api/entries`, {
      method: "POST", headers: hdr({ cookie: cookieA }), body: JSON.stringify({ content: "entryA" }),
    });
    expect(create.status).toBe(201);

    // B sees no entries via API; A sees own
    const listB = await (await fetch(`${baseUrl}/api/entries`, { headers: hdr({ cookie: cookieB }) })).json();
    expect(listB.entries).toHaveLength(0);
    const listA = await (await fetch(`${baseUrl}/api/entries`, { headers: hdr({ cookie: cookieA }) })).json();
    expect(listA.entries).toHaveLength(1);

    // direct SQL with the runtime role: RLS enforced even without app.user_id
    const rt = new Pool({ connectionString: RUNTIME_URL });
    try {
      const aId = usersIdFor(await snapshotAuthDb(), authIdFor(await snapshotAuthDb(), "u1@test.com"))!;
      const bId = usersIdFor(await snapshotAuthDb(), authIdFor(await snapshotAuthDb(), "ub@test.com"))!;
      // without app.user_id -> no rows visible
      expect((await rt.query(`SELECT * FROM entries`)).rowCount).toBe(0);
      // as B: no rows, cannot modify A's row
      await rt.query(`BEGIN`);
      await rt.query(`SELECT set_config('app.user_id', $1, true)`, [bId]);
      expect((await rt.query(`SELECT * FROM entries`)).rowCount).toBe(0);
      expect((await rt.query(`UPDATE entries SET content = 'hacked' WHERE content = 'entryA' RETURNING id`)).rowCount).toBe(0);
      await rt.query(`COMMIT`);
      // as A: sees own row
      await rt.query(`BEGIN`);
      await rt.query(`SELECT set_config('app.user_id', $1, true)`, [aId]);
      expect((await rt.query(`SELECT * FROM entries`)).rowCount).toBe(1);
      await rt.query(`COMMIT`);

      // Migration 0004 regression (gate review IN-02): after that COMMIT the
      // transaction-local setting resets to '' — NOT to NULL — on this very
      // connection. The pre-0004 policy cast '' to uuid and raised
      // "invalid input syntax for type uuid" (a 500 instead of an empty
      // result). NULLIF must turn it back into a fail-closed 0-row read.
      expect((await rt.query(`SELECT current_setting('app.user_id', true)`)).rows[0].current_setting).toBe("");
      expect((await rt.query(`SELECT * FROM entries`)).rowCount).toBe(0);
    } finally {
      await rt.end();
    }
  });

  it("7) every business route rejects unauthenticated; healthz/readyz reachable; spoofed authentik headers ignored", async () => {
    expect((await fetch(`${baseUrl}/api/entries`, { headers: hdr() })).status).toBe(401);
    expect((await fetch(`${baseUrl}/api/me`, { headers: hdr() })).status).toBe(401);
    expect((await fetch(`${baseUrl}/api/entries`, { headers: hdr({ "x-authentik-username": "admin", "x-authentik-email": "admin@test.com" }) })).status).toBe(401);
    expect((await fetch(`${baseUrl}/healthz`)).status).toBe(200);
    expect((await fetch(`${baseUrl}/readyz`)).status).toBe(200);
  });

  it("8) rate limit enforced (429) with DB storage — survives server restart", async () => {
    const fixedIp = "10.99.0.1";
    let last = 0;
    for (let i = 0; i < 15; i++) {
      const res = await fetch(`${baseUrl}/api/auth/sign-in/email`, {
        method: "POST",
        headers: { "content-type": "application/json", "x-forwarded-for": fixedIp },
        body: JSON.stringify({ email: "u1@test.com", password: "wrong-password-123" }),
      });
      last = res.status;
      if (last === 429) break;
    }
    expect(last).toBe(429);

    // DB storage property (claude review 2026-09-19, MEDIUM-6): with in-memory
    // storage a restart would reset the counter and the next attempt would be
    // a fresh 401. With database storage the counter persists and the same IP
    // stays throttled (429) across the restart.
    await stopServer();
    serverProcess = startServer();
    await waitForHealth();
    const res = await fetch(`${baseUrl}/api/auth/sign-in/email`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-forwarded-for": fixedIp },
      body: JSON.stringify({ email: "u1@test.com", password: "wrong-password-123" }),
    });
    expect(res.status).toBe(429);
  });

  it("9) logout invalidates the session", async () => {
    const login = await passwordSignIn("u1@test.com", "correct-horse-battery-staple");
    const cookie = sessionCookie(login.cookie)!;
    expect((await fetch(`${baseUrl}/api/me`, { headers: hdr({ cookie }) })).status).toBe(200);
    const out = await fetch(`${baseUrl}/api/auth/sign-out`, {
      method: "POST", headers: hdr({ cookie }), body: "{}",
    });
    expect(out.status).toBeLessThan(400);
    expect((await fetch(`${baseUrl}/api/me`, { headers: hdr({ cookie }) })).status).toBe(401);
  });

  it("10) planned BETTER_AUTH_SECRETS rotation invalidates old sessions (re-login required); new logins OK", async () => {
    await seedUser("u3@test.com", "U Three", "correct-horse-battery-staple");
    const login = await passwordSignIn("u3@test.com", "correct-horse-battery-staple");
    const cookie = sessionCookie(login.cookie)!;
    expect((await fetch(`${baseUrl}/api/me`, { headers: hdr({ cookie }) })).status).toBe(200);

    // rotation: new secret first (hard cutover). better-auth 1.7.5 signs the
    // session-token cookie with the flat first secret only (live probe,
    // evidence doc): there is NO session-cookie rollover — the old session
    // is invalidated and a re-login is required (decision 2026-09-19).
    currentSecrets = `2:${SECRET_V2},1:${SECRET_V1}`;
    await stopServer();
    serverProcess = startServer();
    await waitForHealth();

    // old session is now invalid
    expect((await fetch(`${baseUrl}/api/me`, { headers: hdr({ cookie }) })).status).toBe(401);
    // new login OK under rotated env
    const relogin = await passwordSignIn("u3@test.com", "correct-horse-battery-staple");
    expect(relogin.status).toBe(200);
  });

  it("11) incident revoke (admin:revoke-sessions) makes captured old cookie fail", async () => {
    const login = await passwordSignIn("u3@test.com", "correct-horse-battery-staple");
    const cookie = sessionCookie(login.cookie)!;
    expect((await fetch(`${baseUrl}/api/me`, { headers: hdr({ cookie }) })).status).toBe(200);

    await revokeSessions("u3@test.com");

    expect((await fetch(`${baseUrl}/api/me`, { headers: hdr({ cookie }) })).status).toBe(401);
  });

  it("12) restart/redeploy without revocation preserves valid sessions", async () => {
    const login = await passwordSignIn("u3@test.com", "correct-horse-battery-staple");
    const cookie = sessionCookie(login.cookie)!;
    await stopServer();
    serverProcess = startServer(); // same secrets, no revoke
    await waitForHealth();
    expect((await fetch(`${baseUrl}/api/me`, { headers: hdr({ cookie }) })).status).toBe(200);
  });

  it("13) unverified Google fixture creates no access and no rows", async () => {
    const before = await snapshotAuthDb();
    const res = await googleFlow(null, "sign-in/social", { loginHint: "gunv" });
    // unverified provider email + implicit sign-up disabled -> refusal, never a session
    expect(sessionCookie(res.headers.get("set-cookie"))).toBeNull();
    const after = await snapshotAuthDb();
    expect(after.user.filter((u) => u.email === "unverified-google@test.com").length).toBe(0);
    // u1 untouched
    expect(usersIdFor(after, authIdFor(before, "u1@test.com"))).toBe(usersIdFor(before, authIdFor(before, "u1@test.com")));
  });

  it("14) runtime role has no privileges on the migration journal (gesamtreview M4)", async () => {
    const { rows } = await ownerPool.query(
      `SELECT has_table_privilege('app_runtime', 'public.__drizzle_migrations', 'SELECT,INSERT,UPDATE,DELETE') AS any_priv`,
    );
    expect(rows[0].any_priv).toBe(false);
    const rt = new Pool({ connectionString: RUNTIME_URL });
    try {
      await expect(rt.query("DELETE FROM public.__drizzle_migrations")).rejects.toThrow(/permission denied/);
    } finally {
      await rt.end();
    }
  });
});