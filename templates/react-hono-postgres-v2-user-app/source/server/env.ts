// Fail-closed boot validation for runtime secrets (Terra-Review 2026-09-19, M1;
// hardened in re-review rounds 2+3: numeric unique canonical versions, mandatory
// validated prod proxies). better-auth 1.7.5 silently falls back to its built-in
// default secret when BETTER_AUTH_SECRETS is missing (create-context.mjs:70-81);
// we refuse to boot instead. Called from server.ts and admin-cli.ts before any
// listening/IO.

// Parse and validate a BETTER_AUTH_TRUSTED_PROXIES value. Absent env → undefined.
// Present-but-invalid (empty/delimiter-only entries, bad CIDR/IP) → throw.
export function parseTrustedProxies(raw: string | undefined): string[] | undefined {
  if (raw === undefined) return undefined;
  const entries = raw.split(",").map((s) => s.trim());
  // Reject empty/delimiter-only entries explicitly — no silent filtering
  // (re-review round 3: ",,," or "   " must fail closed, not yield undefined).
  if (entries.some((e) => e === "")) {
    throw new Error("BETTER_AUTH_TRUSTED_PROXIES contains empty entries (check for stray commas or whitespace-only values)");
  }
  for (const entry of entries) {
    if (!isValidIpOrCidr(entry)) {
      throw new Error(`BETTER_AUTH_TRUSTED_PROXIES contains an invalid IP/CIDR entry (entries are never echoed)`);
    }
  }
  return entries;
}

// IPv4: four strict octets, optional /0-32. IPv6: sane hex/colon/dot form,
// optional /0-128. Better Auth parses via its own CIDR parser; this boot check
// rejects clearly invalid entries before they are silently ignored.
// Strict IP/CIDR validation via Node's net.isIP (Terra re-review 3: loose
// regex accepted invalid IPv6 forms like "::::" / "1:2" / "2001:db8:::1",
// oversized groups like "12345::", invalid embedded IPv4 like
// "::ffff:999.1.1.1", and leading-zero IPv4 like "192.168.001.1"; better-auth
// would then discard them at runtime and production could boot with an
// effectively empty proxy list).
import { isIP } from "node:net";

function isValidIpOrCidr(entry: string): boolean {
  const slash = entry.lastIndexOf("/");
  const addr = slash === -1 ? entry : entry.slice(0, slash);
  const prefix = slash === -1 ? undefined : entry.slice(slash + 1);

  const version = isIP(addr);
  if (version === 0) return false;

  if (prefix !== undefined) {
    if (!/^\d+$/.test(prefix)) return false;
    const bits = Number(prefix);
    const max = version === 6 ? 128 : 32;
    if (!Number.isInteger(bits) || bits < 0 || bits > max) return false;
  }

  return true;
}

// --- Trusted proxies must not swallow the clients themselves ------------
//
// Gate review 2026-09-20 (WR-03). `getIPFromHeader` walks X-Forwarded-For from
// the RIGHT and skips every hop that matches `trustedProxies`, returning the
// first hop that does not. That is correct only as long as the list contains
// proxies. If it also covers the address space the real clients live in, each
// client's own entry is skipped as if it were infrastructure, the walk lands on
// a hop that is identical for everybody, and every client shares one rate-limit
// bucket — a lockout channel, not a per-client limit. The shipped example was
// exactly that: `100.64.0.0/10` is the Tailscale CGNAT range, i.e. the range
// every tailnet client (laptop, phone, mini) has an address in, not a proxy
// range. The failure is silent: the bucket key looks like a real IP, so it does
// not even show up as the `no-trusted-ip` fallback.
//
// The trusted list holds the hop(s) BETWEEN Tailscale and the app — the
// Tailscale→Traefik hop and the Traefik→container hop — and nothing else.
const CLIENT_ADDRESS_SPACES = [
  { cidr: "100.64.0.0/10", label: "the Tailscale tailnet range (CGNAT) — real clients live here, not proxies" },
] as const;

function ipv4Bytes(addr: string): number[] | null {
  const mapped = addr.toLowerCase().startsWith("::ffff:") ? addr.slice(7) : addr;
  if (isIP(mapped) !== 4) return null;
  return mapped.split(".").map(Number);
}

// True when the two IPv4 CIDRs share any address at all (either contains the
// other). Non-IPv4 entries can never overlap the IPv4 client spaces above.
function ipv4CidrsOverlap(a: string, b: string): boolean {
  const split = (entry: string) => {
    const slash = entry.lastIndexOf("/");
    const bytes = ipv4Bytes(slash === -1 ? entry : entry.slice(0, slash));
    const prefix = slash === -1 ? 32 : Number(entry.slice(slash + 1));
    return bytes ? { bytes, prefix } : null;
  };
  const x = split(a);
  const y = split(b);
  if (!x || !y) return false;

  const bits = Math.min(x.prefix, y.prefix);
  for (let i = 0; i < 4; i++) {
    const take = Math.min(8, Math.max(0, bits - i * 8));
    if (take === 0) break;
    const mask = (0xff << (8 - take)) & 0xff;
    if ((x.bytes[i]! & mask) !== (y.bytes[i]! & mask)) return false;
  }
  return true;
}

// Entries that would strip real clients out of the forwarded chain. Returns the
// human-readable reasons, never the entry values themselves.
export function trustedProxyClientSpaceConflicts(entries: string[]): string[] {
  const conflicts: string[] = [];
  for (const space of CLIENT_ADDRESS_SPACES) {
    if (entries.some((entry) => ipv4CidrsOverlap(entry, space.cidr))) {
      conflicts.push(
        `BETTER_AUTH_TRUSTED_PROXIES must not cover ${space.cidr} — ${space.label}. ` +
          "Trusting it makes every client share one rate-limit bucket. " +
          "List only the proxy hops between Tailscale and the container.",
      );
    }
  }
  return conflicts;
}

// Comma-separated CIDR/IP list for advanced.ipAddress.trustedProxies
// (Terra-Review 2026-09-19, M2: multi-hop X-Forwarded-For behind Traefik).
export function trustedProxiesFromEnv(): string[] | undefined {
  return parseTrustedProxies(process.env.BETTER_AUTH_TRUSTED_PROXIES);
}

// Placeholder denylist (claude review 2026-09-19, MEDIUM-1): values that are
// public in this repo (or obvious defaults) must fail the boot even though
// they satisfy the format check. Case-insensitive substring match.
const PLACEHOLDER_PATTERNS = ["replace-with", "changeme", "change-me", "placeholder"];
function isPlaceholderSecret(value: string): boolean {
  const lower = value.toLowerCase();
  return PLACEHOLDER_PATTERNS.some((p) => lower.includes(p));
}

export function validateRuntimeEnv(): void {
  const problems: string[] = [];

  const secrets = process.env.BETTER_AUTH_SECRETS;
  if (!secrets || !secrets.trim()) {
    problems.push("BETTER_AUTH_SECRETS is required (format: '<version>:<secret>[,<version>:<secret>...], canonical numeric versions, unique, first = current)");
  } else {
    const entries = secrets.split(",");
    // Round 3: no silent filtering — empty entries fail closed.
    if (entries.some((e) => e.trim() === "")) {
      problems.push("BETTER_AUTH_SECRETS contains empty entries (check for stray commas)");
    } else {
      const seenVersions = new Set<string>();
      for (const entry of entries) {
        const match = /^(\d+):(.+)$/.exec(entry.trim());
        // Canonical decimal notation: "01" and "1" are the same version in
        // better-auth's numeric parsing — leading zeros are rejected so a
        // textual-uniqueness check cannot miss a numeric duplicate.
        if (!match || !/^(?:0|[1-9]\d*)$/.test(match[1])) {
          problems.push("BETTER_AUTH_SECRETS entry must match '<canonical-numeric-version>:<secret>' (no leading zeros)");
          break;
        }
        if (seenVersions.has(match[1])) {
          problems.push("BETTER_AUTH_SECRETS versions must be unique (duplicates found)");
          break;
        }
        // Claude review 2026-09-19 (MEDIUM-1): format validity alone accepted
        // placeholder secrets copied from the checked-in .env.example
        // ("1:replace-with-generated-secret" booted). A repo placeholder is
        // as publicly known as better-auth's default secret — enforce strength.
        if (match[2].length < 32) {
          problems.push("BETTER_AUTH_SECRETS secret values must be at least 32 characters");
          break;
        }
        if (isPlaceholderSecret(match[2])) {
          problems.push("BETTER_AUTH_SECRETS contains a placeholder value (generate a real secret, never copy .env.example)");
          break;
        }
        seenVersions.add(match[1]);
      }
    }
  }

  if (!process.env.BETTER_AUTH_URL) {
    problems.push("BETTER_AUTH_URL is required");
  }

  if (!process.env.DATABASE_URL) {
    problems.push("DATABASE_URL is required");
  }

  // Production hardening is keyed on "not explicitly development/test"
  // (claude review 2026-09-19, MEDIUM-2): an exact `NODE_ENV === "production"`
  // check booted the app without OAuth credentials and without the mandatory
  // proxy list when Coolify overwrote NODE_ENV (e.g. "prod", "Production").
  // Everything that is not clearly dev/test is treated as hardened prod —
  // fail closed, never silently less strict.
  const isProductionLike =
    process.env.NODE_ENV !== "development" && process.env.NODE_ENV !== "test";

  if (isProductionLike) {
    if (!process.env.GOOGLE_CLIENT_ID || !process.env.GOOGLE_CLIENT_SECRET) {
      problems.push("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are required in production");
    }

    // Claude review 2026-09-19 (MEDIUM-3): TEST_OIDC_ISSUER swaps the Google
    // provider for a generic OIDC provider against an arbitrary discovery URL
    // at runtime — a test hook must never reach a production boot.
    if (process.env.TEST_OIDC_ISSUER !== undefined) {
      problems.push("TEST_OIDC_ISSUER must not be set outside tests (it overrides the Google provider with an arbitrary discovery URL)");
    }
    // Terra re-review 2026-09-19 (M2, rounds 2+3): every production user-app
    // deploys behind Traefik (multi-hop X-Forwarded-For). The proxy CIDR list
    // is MANDATORY and must be present, non-empty and valid — the parsed
    // result is checked, not the raw string's truthiness. Dev/test runs
    // (direct localhost, NODE_ENV unset) may omit it. NOTE: with the
    // production-like keying above, NODE_ENV unset is now ALSO hardened
    // (fail closed); explicit development/test stays exempt.
    const rawProxies = process.env.BETTER_AUTH_TRUSTED_PROXIES;
    if (rawProxies === undefined) {
      problems.push("BETTER_AUTH_TRUSTED_PROXIES is required in production (comma-separated Traefik CIDR/IP list)");
    } else {
      try {
        const parsed = parseTrustedProxies(rawProxies);
        if (!parsed || parsed.length === 0) {
          problems.push("BETTER_AUTH_TRUSTED_PROXIES must contain at least one valid CIDR/IP entry in production");
        } else {
          // Gate review 2026-09-20 (WR-03): fail closed on a list that covers
          // the clients themselves — see CLIENT_ADDRESS_SPACES above.
          problems.push(...trustedProxyClientSpaceConflicts(parsed));
        }
      } catch (err) {
        problems.push(err instanceof Error ? err.message : "BETTER_AUTH_TRUSTED_PROXIES is invalid");
      }
    }
  }

  if (problems.length > 0) {
    // No secret values are ever echoed — names only.
    throw new Error(`runtime env validation failed:\n  - ${problems.join("\n  - ")}`);
  }
}