import { describe, it, expect, afterEach } from "vitest";
import { createHash } from "node:crypto";
import {
  proxySecretConfigured,
  verifyProxySecretHeader,
} from "../source/server/proxySecret.js";

// Proxy-secret verification (Studio App Contract v2, G2):
// SHA-256(X-Studio-Proxy-Secret) must match STUDIO_PROXY_SECRET_SHA256,
// length-checked, timing-safe, fail-closed without a valid digest env var.
// Rotation (review B2): the env var accepts a comma-separated digest list.
// The digest is read from process.env at call time, so tests set/restore it.

const SECRET = "unit-test-plaintext-secret-value-0123456789abcdef";
const SECRET_DIGEST = createHash("sha256").update(SECRET).digest("hex");
const OLD_SECRET = "unit-test-old-plaintext-secret-0123456789abcdef";
const OLD_DIGEST = createHash("sha256").update(OLD_SECRET).digest("hex");

const prev = process.env.STUDIO_PROXY_SECRET_SHA256;
afterEach(() => {
  if (prev === undefined) {
    delete process.env.STUDIO_PROXY_SECRET_SHA256;
  } else {
    process.env.STUDIO_PROXY_SECRET_SHA256 = prev;
  }
});

describe("verifyProxySecretHeader (G2)", () => {
  it("accepts the correct plaintext header when configured", () => {
    process.env.STUDIO_PROXY_SECRET_SHA256 = SECRET_DIGEST;
    expect(verifyProxySecretHeader(SECRET)).toBe(true);
  });

  it("rejects a wrong plaintext", () => {
    process.env.STUDIO_PROXY_SECRET_SHA256 = SECRET_DIGEST;
    expect(verifyProxySecretHeader("wrong-secret")).toBe(false);
  });

  it("rejects digest-as-bearer (the digest itself is not a valid secret)", () => {
    process.env.STUDIO_PROXY_SECRET_SHA256 = SECRET_DIGEST;
    expect(verifyProxySecretHeader(SECRET_DIGEST)).toBe(false);
  });

  it("rejects missing/empty/oversized headers", () => {
    process.env.STUDIO_PROXY_SECRET_SHA256 = SECRET_DIGEST;
    expect(verifyProxySecretHeader(undefined)).toBe(false);
    expect(verifyProxySecretHeader("")).toBe(false);
    expect(verifyProxySecretHeader("x".repeat(2000))).toBe(false);
  });

  it("fails closed without configured digest", () => {
    delete process.env.STUDIO_PROXY_SECRET_SHA256;
    expect(proxySecretConfigured()).toBe(false);
    expect(verifyProxySecretHeader(SECRET)).toBe(false);
  });

  it("fails closed with malformed digest", () => {
    process.env.STUDIO_PROXY_SECRET_SHA256 = "zz".repeat(32);
    expect(proxySecretConfigured()).toBe(false);
    expect(verifyProxySecretHeader(SECRET)).toBe(false);
  });

  it("rotation (B2): accepts new and old plaintext with a comma-separated digest list", () => {
    process.env.STUDIO_PROXY_SECRET_SHA256 = `${SECRET_DIGEST},${OLD_DIGEST}`;
    expect(proxySecretConfigured()).toBe(true);
    expect(verifyProxySecretHeader(SECRET)).toBe(true);
    expect(verifyProxySecretHeader(OLD_SECRET)).toBe(true);
  });

  it("rotation (B2): tolerates whitespace around list entries", () => {
    process.env.STUDIO_PROXY_SECRET_SHA256 = ` ${SECRET_DIGEST} , ${OLD_DIGEST} `;
    expect(verifyProxySecretHeader(SECRET)).toBe(true);
    expect(verifyProxySecretHeader(OLD_SECRET)).toBe(true);
  });

  it("rotation (B2): malformed entries are ignored, valid ones still work", () => {
    process.env.STUDIO_PROXY_SECRET_SHA256 = `${SECRET_DIGEST},zzzz`;
    expect(verifyProxySecretHeader(SECRET)).toBe(true);
    expect(verifyProxySecretHeader("zzzz")).toBe(false);
  });

  it("contract: uppercase-hex digests fail closed (lowercase hex only)", () => {
    process.env.STUDIO_PROXY_SECRET_SHA256 = SECRET_DIGEST.toUpperCase();
    expect(proxySecretConfigured()).toBe(false);
    expect(verifyProxySecretHeader(SECRET)).toBe(false);
  });
});