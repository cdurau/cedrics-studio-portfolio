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

  it("@node-rs/argon2 is pinned exactly to 2.2.1", () => {
    const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
    expect(pkg.dependencies["@node-rs/argon2"]).toBe("2.2.1");
    const lock = readFileSync(new URL("../package-lock.json", import.meta.url), "utf8");
    expect(lock).toContain('"@node-rs/argon2": "2.2.1"');
    const installed = execSync("npm ls @node-rs/argon2 --json", {
      cwd: new URL("..", import.meta.url).pathname,
    }).toString();
    expect(JSON.parse(installed).dependencies["@node-rs/argon2"].version).toBe("2.2.1");
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

  it("env validation: BETTER_AUTH_SECRETS rejects malformed/duplicate/empty entries", async () => {
    const { validateRuntimeEnv } = await import("../source/server/env.js");
    // ≥32 chars, non-placeholder: satisfies the strength rules so the
    // assertions below test the intended failure modes, not secret strength.
    const LONG_A = "contract-secret-a-0123456789abcdef0123456789abcdef";
    const LONG_B = "contract-secret-b-0123456789abcdef0123456789abcdef";
    const base = {
      BETTER_AUTH_SECRETS: `1:${LONG_A}`,
      BETTER_AUTH_URL: "https://x",
      DATABASE_URL: "postgresql://x",
    } as Record<string, string>;
    const run = (env: Record<string, string>) => {
      const saved = { ...process.env };
      try {
        Object.assign(process.env, base, env);
        // simulate non-production
        process.env.NODE_ENV = "test";
        delete process.env.TEST_OIDC_ISSUER;
        validateRuntimeEnv();
        return "ok";
      } catch {
        return "rejected";
      } finally {
        // LOW-8: the previous version only re-assigned `saved`, so keys that
        // did not exist before (base + env) stayed set for following tests.
        // Clear everything first, then restore the snapshot.
        for (const k of Object.keys(process.env)) delete process.env[k];
        Object.assign(process.env, saved);
      }
    };
    expect(run({})).toBe("ok");
    expect(run({ BETTER_AUTH_SECRETS: "" })).toBe("rejected");
    // leading zeros: "01" collides numerically with "1" → rejected (round 3)
    expect(run({ BETTER_AUTH_SECRETS: `1:${LONG_A},01:${LONG_B}` })).toBe("rejected");
    // duplicate version → rejected
    expect(run({ BETTER_AUTH_SECRETS: `1:${LONG_A},1:${LONG_B}` })).toBe("rejected");
    // empty CSV entry → rejected (no silent filtering)
    expect(run({ BETTER_AUTH_SECRETS: `1:${LONG_A},` })).toBe("rejected");
    // non-numeric version → rejected
    expect(run({ BETTER_AUTH_SECRETS: `one:${LONG_A}` })).toBe("rejected");
    // secret strength (claude review 2026-09-19, MEDIUM-1): too short → rejected
    expect(run({ BETTER_AUTH_SECRETS: "1:short" })).toBe("rejected");
    // placeholder values (public in the repo) → rejected even at length
    expect(run({ BETTER_AUTH_SECRETS: "1:replace-with-generated-secret-aaaaaaaaaaaaaaaaaaaaaa" })).toBe("rejected");
    expect(run({ BETTER_AUTH_SECRETS: "1:changeme-secret-aaaaaaaaaaaaaaaaaaaaaaaaaaaaa" })).toBe("rejected");
    // a 32+ char non-placeholder value passes
    expect(run({ BETTER_AUTH_SECRETS: "1:" + "x".repeat(32) })).toBe("ok");
  });

  it("env validation: trustedProxies parsing fails closed on empty/invalid entries", async () => {
    const { parseTrustedProxies } = await import("../source/server/env.js");
    expect(parseTrustedProxies(undefined)).toBeUndefined();
    expect(parseTrustedProxies("100.64.0.0/10")).toEqual(["100.64.0.0/10"]);
    expect(parseTrustedProxies("100.64.0.0/10, 172.18.0.0/16")).toHaveLength(2);
    // whitespace-only / delimiter-only → rejected, never undefined (round 3)
    expect(() => parseTrustedProxies("   ")).toThrow();
    expect(() => parseTrustedProxies(",,,")).toThrow();
    expect(() => parseTrustedProxies("not-a-cidr")).toThrow();
    // trailing comma → rejected (empty entry)
    expect(() => parseTrustedProxies("100.64.0.0/10,")).toThrow();
    // prefix bounds
    expect(() => parseTrustedProxies("100.64.0.0/33")).toThrow();
    expect(() => parseTrustedProxies("10.0.0.0/32")).not.toThrow();
    // strict IP forms (terra re-review 3): invalid IPv6 shapes, oversized
    // groups, invalid embedded IPv4, leading-zero IPv4 → all rejected
    expect(() => parseTrustedProxies("::::")).toThrow();
    expect(() => parseTrustedProxies("1:2")).toThrow();
    expect(() => parseTrustedProxies("2001:db8:::1")).toThrow();
    expect(() => parseTrustedProxies("12345::")).toThrow();
    expect(() => parseTrustedProxies("::ffff:999.1.1.1")).toThrow();
    expect(() => parseTrustedProxies("192.168.001.1")).toThrow();
    // valid forms must keep passing
    expect(parseTrustedProxies("2001:db8::/32")).toEqual(["2001:db8::/32"]);
    expect(parseTrustedProxies("::ffff:100.64.0.1")).toEqual(["::ffff:100.64.0.1"]);
  });

  it("env validation: production requires validated non-empty trustedProxies", async () => {
    const { validateRuntimeEnv } = await import("../source/server/env.js");
    const base = {
      NODE_ENV: "production",
      BETTER_AUTH_SECRETS: "1:contract-secret-a-0123456789abcdef0123456789abcdef",
      BETTER_AUTH_URL: "https://x",
      DATABASE_URL: "postgresql://x",
      GOOGLE_CLIENT_ID: "id",
      GOOGLE_CLIENT_SECRET: "secret",
    } as Record<string, string>;
    const run = (proxies: string | undefined) => {
      const saved = { ...process.env };
      try {
        Object.assign(process.env, base);
        if (proxies === undefined) delete process.env.BETTER_AUTH_TRUSTED_PROXIES;
        else process.env.BETTER_AUTH_TRUSTED_PROXIES = proxies;
        validateRuntimeEnv();
        return "ok";
      } catch {
        return "rejected";
      } finally {
        delete process.env.BETTER_AUTH_TRUSTED_PROXIES;
        Object.assign(process.env, saved);
      }
    };
    expect(run(undefined)).toBe("rejected");
    expect(run("   ")).toBe("rejected");
    expect(run(",,,")).toBe("rejected");
    expect(run("not-a-cidr")).toBe("rejected");
    expect(run("100.64.0.0/10")).toBe("ok");
  });

  it("env validation: production-like NODE_ENV spellings are all hardened (MEDIUM-2)", async () => {
    const { validateRuntimeEnv } = await import("../source/server/env.js");
    // Missing Google creds must be rejected for EVERY production-like NODE_ENV
    // value — including NODE_ENV unset (fail closed, not fail open).
    for (const nodeEnv of ["production", "Production", "prod", "PROD", undefined]) {
      const saved = { ...process.env };
      try {
        if (nodeEnv === undefined) delete process.env.NODE_ENV;
        else process.env.NODE_ENV = nodeEnv;
        process.env.BETTER_AUTH_SECRETS = "1:" + "x".repeat(32);
        process.env.BETTER_AUTH_URL = "https://x";
        process.env.DATABASE_URL = "postgresql://x";
        delete process.env.GOOGLE_CLIENT_ID;
        delete process.env.GOOGLE_CLIENT_SECRET;
        delete process.env.BETTER_AUTH_TRUSTED_PROXIES;
        expect(() => validateRuntimeEnv()).toThrow();
      } finally {
        Object.assign(process.env, saved);
      }
    }
    // explicit development/test without credentials is allowed
    for (const nodeEnv of ["development", "test"]) {
      const saved = { ...process.env };
      try {
        process.env.NODE_ENV = nodeEnv;
        process.env.BETTER_AUTH_SECRETS = "1:" + "x".repeat(32);
        process.env.BETTER_AUTH_URL = "https://x";
        process.env.DATABASE_URL = "postgresql://x";
        validateRuntimeEnv(); // must not throw
      } finally {
        Object.assign(process.env, saved);
      }
    }
  });

  // HIGH-3 (claude re-review 2026-09-19): the placeholder denylist in env.ts
  // and the placeholder actually written in .env.example drifted apart in the
  // same commit — the denylist matched "replace-with"/"changeme" while
  // .env.example shipped "generate-a-real-secret-at-least-32-chars-long",
  // which passed every check and booted. Asserting a COPY of the placeholder
  // cannot catch that. This test reads the shipped file, so the denylist and
  // the file can never drift apart again.
  it("the placeholder actually shipped in .env.example is refused at boot (HIGH-3)", async () => {
    const { validateRuntimeEnv } = await import("../source/server/env.js");
    const envExample = readFileSync(new URL("../.env.example", import.meta.url), "utf8");

    const line = envExample
      .split("\n")
      .map((l) => l.trim())
      .find((l) => l.startsWith("BETTER_AUTH_SECRETS="));
    expect(line, ".env.example must document BETTER_AUTH_SECRETS").toBeDefined();

    const shipped = line!.slice("BETTER_AUTH_SECRETS=".length).trim();
    expect(shipped, "the documented value must not be empty").toBeTruthy();

    const saved = { ...process.env };
    try {
      process.env.NODE_ENV = "test";
      process.env.BETTER_AUTH_URL = "https://x";
      process.env.DATABASE_URL = "postgresql://x";
      delete process.env.TEST_OIDC_ISSUER;
      process.env.BETTER_AUTH_SECRETS = shipped;
      // Whoever copies .env.example unchanged must not get a booting app:
      // a value published in this repository is as public as a default secret.
      expect(() => validateRuntimeEnv()).toThrow();
    } finally {
      for (const k of Object.keys(process.env)) delete process.env[k];
      Object.assign(process.env, saved);
    }
  });

  it("env validation: TEST_OIDC_ISSUER is rejected in production-like boots (MEDIUM-3)", async () => {
    const { validateRuntimeEnv } = await import("../source/server/env.js");
    const saved = { ...process.env };
    try {
      process.env.NODE_ENV = "production";
      process.env.BETTER_AUTH_SECRETS = "1:" + "x".repeat(32);
      process.env.BETTER_AUTH_URL = "https://x";
      process.env.DATABASE_URL = "postgresql://x";
      process.env.GOOGLE_CLIENT_ID = "id";
      process.env.GOOGLE_CLIENT_SECRET = "secret";
      process.env.BETTER_AUTH_TRUSTED_PROXIES = "100.64.0.0/10";
      process.env.TEST_OIDC_ISSUER = "http://boese.example";
      expect(() => validateRuntimeEnv()).toThrow(/TEST_OIDC_ISSUER/);
      delete process.env.TEST_OIDC_ISSUER;
      expect(() => validateRuntimeEnv()).not.toThrow();
    } finally {
      Object.assign(process.env, saved);
    }
  });
});