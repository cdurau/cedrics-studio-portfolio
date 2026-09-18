import { createHash, timingSafeEqual } from "node:crypto";

// Studio App Contract v2 (G2): the app verifies
// SHA-256(X-Studio-Proxy-Secret) == STUDIO_PROXY_SECRET_SHA256.
// The digest env var is non-secret by construction; the plaintext secret is
// never present in the app (env, image, or config). Fail-closed: if the env
// var is missing or malformed, every proxy-secret check fails (no bypass).

const DIGEST_RE = /^[0-9a-f]{64}$/;

// Read at call time; the runtime env is constant for the process lifetime.
export function getConfiguredDigest(): string {
  return (process.env.STUDIO_PROXY_SECRET_SHA256 ?? "").trim();
}

export function proxySecretConfigured(): boolean {
  return DIGEST_RE.test(getConfiguredDigest());
}

// Header length is checked before hashing to avoid unbounded work; the hash
// comparison itself is timing-safe over fixed-size 32-byte digests.
export function verifyProxySecretHeader(
  headerValue: string | undefined,
): boolean {
  const configured = getConfiguredDigest();
  if (!DIGEST_RE.test(configured)) {
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
  const expected = Buffer.from(configured, "hex");
  return timingSafeEqual(actual, expected);
}