// Fail-closed boot validation for runtime secrets (Terra-Review 2026-09-19, M1).
// better-auth 1.7.5 silently falls back to its built-in default secret when
// BETTER_AUTH_SECRETS is missing (create-context.mjs:70-81); we refuse to boot
// instead. Called from server.ts and admin-cli.ts before any listening/IO.

const SECRET_ENTRY = /^[^:]+:.+$/;

export function validateRuntimeEnv(): void {
  const problems: string[] = [];

  const secrets = process.env.BETTER_AUTH_SECRETS;
  if (!secrets || !secrets.trim()) {
    problems.push("BETTER_AUTH_SECRETS is required (format: '<version>:<secret>[,<version>:<secret>...]')");
  } else {
    const entries = secrets.split(",").map((s) => s.trim()).filter(Boolean);
    if (entries.length === 0 || entries.some((e) => !SECRET_ENTRY.test(e))) {
      problems.push("BETTER_AUTH_SECRETS entries must match '<version>:<secret>' (comma-separated; first entry = current)");
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