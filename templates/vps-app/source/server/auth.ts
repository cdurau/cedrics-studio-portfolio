import { betterAuth } from "better-auth";
import { drizzleAdapter } from "better-auth/adapters/drizzle";
import { db } from "./db/client.js";
import * as authSchema from "./auth-schema.js";
import { hash, verify } from "@node-rs/argon2";
import { trustedProxiesFromEnv } from "./env.js";

// Argon2id parameters (RFC 9106 recommended profile).
// Format: $argon2id$v=19$m=19456,t=2,p=1$<salt>$<hash> — Appwrite-import
// compatible (Appwrite uses the same PHC string format).
const argon2Params = { memoryCost: 19456, timeCost: 2, outputLen: 32, parallelism: 1 } as const;

export const auth = betterAuth({
  appName: "studio-user-app",
  // BETTER_AUTH_SECRETS is read natively by better-auth 1.7.5
  // (dist/context/create-context.mjs:70 → parseSecretsEnv).
  // Format: comma-separated "<version>:<secret>", FIRST entry = current.
  // Do NOT split/derive a single `secret` here — that breaks versioned
  // rollover (pre-verification evidence, commit 94f279c).
  database: drizzleAdapter(db, {
    provider: "pg",
    schema: {
      user: authSchema.authUser,
      session: authSchema.authSession,
      account: authSchema.authAccount,
      verification: authSchema.authVerification,
      rateLimit: authSchema.authRateLimit,
    },
  }),
  emailAndPassword: {
    enabled: true,
    // Sign-up closed: users are admin-seeded only (CONTRACTS-v2 user-app auth contract).
    disableSignUp: true,
    requireEmailVerification: true,
    password: {
      hash: async (password) => hash(password, argon2Params),
      verify: async (data) => verify(data.hash, data.password),
    },
  },
  account: { accountLinking: { enabled: false } },
  session: {
    // Incident-revoke must be immediate: no cookie-cached session validity.
    cookieCache: { enabled: false },
  },
  rateLimit: {
    // DB storage survives Blue/Green redeploys (CONTRACTS-v2). Explicitly
    // enabled — the 1.7.5 default is enabled only in production.
    enabled: true,
    storage: "database",
    window: 60,
    max: 100,
    // Custom-rule keys are matched against the basePath-normalized pathname
    // (rate-limiter strips the /api/auth basePath before matching),
    // so keys must NOT carry the /api/auth prefix.
    customRules: {
      "/sign-in/email": { window: 60, max: 10 },
    },
  },
  advanced: {
    cookiePrefix: "user-app",
    defaultCookieAttributes: {
      secure: true,
      httpOnly: true,
      sameSite: "lax",
    },
    // Terra-Review 2026-09-19 (M2): behind Traefik every request carries a
    // multi-hop X-Forwarded-For. Without trustedProxies, @better-auth/core
    // (ip.mjs getIPFromHeader) returns null for multi-value chains and the
    // rate limiter collapses into the global "no-trusted-ip" bucket —
    // fail-safe against brute force, but one bad client can lock out every
    // login. Configure the Traefik CIDR via BETTER_AUTH_TRUSTED_PROXIES;
    // only single-hop direct deployments may omit it.
    ...(trustedProxiesFromEnv() ? { ipAddress: { trustedProxies: trustedProxiesFromEnv() } } : {}),
  },
  databaseHooks: {
    user: {
      create: {
        after: async (user) => {
          // Identity mapping: app-owned users(id uuid) ← auth_user_id.
          // ON CONFLICT safe (unique auth_user_id): a retried hook or an
          // already-mapped user must not abort the create flow.
          await db.insert(authSchema.users).values({ authUserId: user.id }).onConflictDoNothing();
        },
      },
    },
  },
});