// Fail-closed boot validation for runtime secrets (Terra-Review 2026-09-19, M1;
// hardened in re-review round 2: numeric unique versions, mandatory prod proxies).
// better-auth 1.7.5 silently falls back to its built-in default secret when
// BETTER_AUTH_SECRETS is missing (create-context.mjs:70-81); we refuse to boot
// instead. Called from server.ts and admin-cli.ts before any listening/IO.

export function validateRuntimeEnv(): void {
  const problems: string[] = [];

  const secrets = process.env.BETTER_AUTH_SECRETS;
  if (!secrets || !secrets.trim()) {
    problems.push("BETTER_AUTH_SECRETS is required (format: '<version>:<secret>[,<version>:<secret>...], versions numeric and unique, first = current)");
  } else {
    const entries = secrets.split(",").map((s) => s.trim()).filter(Boolean);
    const versions: string[] = [];
    for (const entry of entries) {
      // Better Auth requires numeric, unique versions; reject anything else
      // at boot instead of failing later inside the auth stack (re-review LOW).
      const match = /^(\d+):(.+)$/.exec(entry);
      if (!match || !match[2].trim()) {
        problems.push(`BETTER_AUTH_SECRETS entry must match '<numeric-version>:<secret>': found malformed entry`);
        break;
      }
      versions.push(match[1]);
    }
    if (versions.length > 0) {
      const dupes = versions.filter((v, i) => versions.indexOf(v) !== i);
      if (dupes.length > 0) {
        problems.push("BETTER_AUTH_SECRETS versions must be unique (duplicates found)");
      }
    }
    if (entries.length === 0) {
      problems.push("BETTER_AUTH_SECRETS must contain at least one '<version>:<secret>' entry");
    }
  }

  if (!process.env.BETTER_AUTH_URL) {
    problems.push("BETTER_AUTH_URL is required");
  }

  if (!process.env.DATABASE_URL) {
    problems.push("DATABASE_URL is required");
  }

  if (process.env.NODE_ENV === "production") {
    if (!process.env.GOOGLE_CLIENT_ID || !process.env.GOOGLE_CLIENT_SECRET) {
      problems.push("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are required in production");
    }
    // Terra re-review 2026-09-19 (M2 round 2): every production user-app
    // deploys behind Traefik (multi-hop X-Forwarded-For). Without
    // trustedProxies the rate limiter collapses into the global
    // "no-trusted-ip" bucket — so the proxy CIDR list is MANDATORY here,
    // not optional. Dev/test runs (direct localhost) may omit it.
    if (!process.env.BETTER_AUTH_TRUSTED_PROXIES) {
      problems.push("BETTER_AUTH_TRUSTED_PROXIES is required in production (comma-separated Traefik CIDR/IP list)");
    }
  }

  if (problems.length > 0) {
    // No secret values are ever echoed — names only.
    throw new Error(`runtime env validation failed:\n  - ${problems.join("\n  - ")}`);
  }
}

// Comma-separated CIDR/IP list for advanced.ipAddress.trustedProxies
// (Terra-Review 2026-09-19, M2: multi-hop X-Forwarded-For behind Traefik).
export function trustedProxiesFromEnv(): string[] | undefined {
  const raw = process.env.BETTER_AUTH_TRUSTED_PROXIES;
  if (!raw || !raw.trim()) return undefined;
  const entries = raw.split(",").map((s) => s.trim()).filter(Boolean);
  return entries.length > 0 ? entries : undefined;
}