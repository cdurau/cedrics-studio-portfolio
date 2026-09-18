import { createHash, timingSafeEqual } from "node:crypto";

// Studio App Contract v2 (G2): the app verifies
// SHA-256(X-Studio-Proxy-Secret) against STUDIO_PROXY_SECRET_SHA256.
// The digest env var is non-secret by construction; the plaintext secret is
// never present in the app (env, image, or config). Fail-closed: if the env
// var is missing or malformed, every proxy-secret check fails (no bypass).
//
// Rotation (review B2): the env var accepts a comma-separated list of
// 64-hex digests, e.g. "<new>,<old>" during the rotation window. Every
// entry is compared timing-safe over fixed 32-byte digests. Digests must be
// lowercase hex (contract: templates/CONTRACTS-v2.md, G2); uppercase or
// malformed entries fail closed.

const DIGEST_RE = /^[0-9a-f]{64}$/;

// Read at call time; the runtime env is constant for the process lifetime.
export function getConfiguredDigests(): string[] {
  return (process.env.STUDIO_PROXY_SECRET_SHA256 ?? "")
    .split(",")
    .map((d) => d.trim())
    .filter((d) => d.length > 0);
}

export function proxySecretConfigured(): boolean {
  return getConfiguredDigests().some((d) => DIGEST_RE.test(d));
}

// Header length is checked before hashing to avoid unbounded work; each hash
// comparison is timing-safe over fixed-size 32-byte digests.
export function verifyProxySecretHeader(
  headerValue: string | undefined,
): boolean {
  const configured = getConfiguredDigests().filter((d) => DIGEST_RE.test(d));
  if (configured.length === 0) {
    return false;
  }
  if (
    typeof headerValue !== "string" ||
    headerValue.length === 0 ||
    headerValue.length > 1024
  ) {
    return false;
  }
  const actual = createHash("sha256").update(headerValue, "utf8").digest();
  return configured.some((d) =>
    timingSafeEqual(actual, Buffer.from(d, "hex")),
  );
}