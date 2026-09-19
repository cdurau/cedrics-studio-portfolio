import { describe, it, expect } from "vitest";
import { execSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// Contract unit tests (CONTRACTS-v2 user-app auth contract, static parts).
// Behavioral parts live in tests/integration.test.ts against ephemeral Postgres.

describe("user-app auth contract (static)", () => {
  it("better-auth is pinned exactly to 1.7.5 (no range)", () => {
    const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
    expect(pkg.dependencies["better-auth"]).toBe("1.7.5");
    // Lockfile must carry the exact resolved version too
    const lock = readFileSync(new URL("../package-lock.json", import.meta.url), "utf8");
    expect(lock).toContain('"better-auth": "1.7.5"');
    const installed = execSync("npm ls better-auth --json", {
      cwd: new URL("..", import.meta.url).pathname,
    }).toString();
    expect(JSON.parse(installed).dependencies["better-auth"].version).toBe("1.7.5");
  });

  it("@node-rs/argon2 is pinned exactly", () => {
    const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
    expect(pkg.dependencies["@node-rs/argon2"]).toMatch(/^\d+\.\d+\.\d+$/);
  });

  it("auth config: implicit linking disabled, sign-up closed, cookie cache off, DB rate limit on", async () => {
    const { auth } = await import("../source/server/auth.js");
    const o = auth.options as Record<string, any>;
    expect(o.account?.accountLinking?.disableImplicitLinking).toBe(true);
    expect(o.emailAndPassword?.disableSignUp).toBe(true);
    expect(o.emailAndPassword?.requireEmailVerification).toBe(true);
    expect(o.session?.cookieCache?.enabled).toBe(false);
    expect(o.rateLimit?.enabled).toBe(true);
    expect(o.rateLimit?.storage).toBe("database");
    // stricter rules on sign-in endpoints, basePath-normalized keys
    expect(o.rateLimit?.customRules?.["/sign-in/email"]).toEqual({ window: 60, max: 10 });
  });

  it("no Authentik/proxy-secret header is ever evaluated (source scan)", async () => {
    const { readFileSync, readdirSync, statSync } = await import("node:fs");
    const dir = new URL("../source", import.meta.url).pathname;
    const files: string[] = [];
    const walk = (d: string) => {
      for (const f of readdirSync(d)) {
        const p = `${d}/${f}`;
        if (statSync(p).isDirectory()) walk(p);
        else if (f.endsWith(".ts") || f.endsWith(".tsx")) files.push(p);
      }
    };
    walk(dir);
    for (const f of files) {
      const src = readFileSync(f, "utf8");
      expect(src).not.toMatch(/x-authentik-/i);
      expect(src).not.toMatch(/X-Studio-Proxy-Secret/i);
      expect(src).not.toMatch(/STUDIO_PROXY_SECRET/i);
    }
  });

  it("migration 0002 contains no literals for role passwords", () => {
    const sql = readFileSync(new URL("../drizzle/migrations/0002_rls_and_grants.sql", import.meta.url), "utf8");
    expect(sql).not.toMatch(/CREATE ROLE .* PASSWORD '.*'/);
    expect(sql).toContain("FORCE ROW LEVEL SECURITY");
    expect(sql).toContain("app_runtime");
  });
});