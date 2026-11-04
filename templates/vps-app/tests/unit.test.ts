import { describe, it, expect } from "vitest";
import { bootstrapUrls } from "../source/server/bootstrap.js";
describe("VPS environment", () => {
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
    // parseTrustedProxies only validates FORM. 100.64.0.0/10 is well-formed
    // and still refused by the production boot check (see WR-03 test below) —
    // the two layers are deliberately separate.
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
    // Proxy hops between Tailscale and the container — the intended shape.
    expect(run("127.0.0.1/32")).toBe("ok");
    expect(run("127.0.0.1/32,172.18.0.0/16")).toBe("ok");
    // Gate review 2026-09-20 (WR-03): a list covering the Tailscale client
    // range must fail the boot. It used to be the SHIPPED example, it looked
    // valid, and it silently collapsed every client into one rate-limit
    // bucket — the review reproduced the lockout with two real devices.
    expect(run("100.64.0.0/10")).toBe("rejected");
    expect(run("127.0.0.1/32,100.64.0.0/10")).toBe("rejected");
    expect(run("100.100.0.0/16")).toBe("rejected");
    expect(run("0.0.0.0/0")).toBe("rejected");
    expect(run("100.111.45.149")).toBe("rejected");
  });

  it("trusted proxies: client-space conflicts are detected without echoing entries (WR-03)", async () => {
    const { trustedProxyClientSpaceConflicts } = await import("../source/server/env.js");
    expect(trustedProxyClientSpaceConflicts(["127.0.0.1/32", "172.18.0.0/16"])).toEqual([]);
    expect(trustedProxyClientSpaceConflicts(["10.0.0.0/8"])).toEqual([]);
    expect(trustedProxyClientSpaceConflicts(["100.32.0.0/12"])).toEqual([]); // 100.32-100.47: outside CGNAT
    // a supernet that CONTAINS the client range is just as wrong as the range itself
    expect(trustedProxyClientSpaceConflicts(["100.0.0.0/8"])).toHaveLength(1);
    expect(trustedProxyClientSpaceConflicts(["100.64.0.0/10"])).toHaveLength(1);
    expect(trustedProxyClientSpaceConflicts(["100.127.255.255"])).toHaveLength(1);
    expect(trustedProxyClientSpaceConflicts(["::ffff:100.64.0.1"])).toHaveLength(1);
    // the message names the range and the consequence, never the entry itself
    const [msg] = trustedProxyClientSpaceConflicts(["100.64.0.0/10", "172.18.0.0/16"]);
    expect(msg).toContain("100.64.0.0/10");
    expect(msg).toContain("rate-limit bucket");
    expect(msg).not.toContain("172.18.0.0/16");
  });


});
describe("bootstrap URL handoff", () => {
  const env = { DATABASE_URL: `postgresql://app_runtime:${"a".repeat(64)}@db:5432/postgres`, MIGRATION_DATABASE_URL: "postgresql://postgres:owner@db:5432/postgres" };
  it("preserves exact broker password", () => expect(bootstrapUrls(env).runtime.password).toBe("a".repeat(64)));
  it.each([
    { DATABASE_URL: env.DATABASE_URL.replace("app_runtime", "postgres") },
    { MIGRATION_DATABASE_URL: env.MIGRATION_DATABASE_URL.replace("@db", "@other") },
    { DATABASE_URL: env.DATABASE_URL.replace("/postgres", "/other") },
    { DATABASE_URL: env.DATABASE_URL + "?options=-crole=postgres" },
    { DATABASE_URL: env.DATABASE_URL.replace("a".repeat(64), "short") },
  ])("refuses unsafe URL", change => {
    expect(() => bootstrapUrls({ ...env, ...change })).toThrow("INVALID_BOOTSTRAP_URLS");
  });
});
