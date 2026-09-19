import { betterAuth } from "better-auth";
import { drizzleAdapter } from "better-auth/adapters/drizzle";
import { genericOAuth } from "better-auth/plugins";
import { db } from "./db/client.js";
import * as authSchema from "./db/schema.js";
import { hash, verify } from "@node-rs/argon2";
import { trustedProxiesFromEnv } from "./env.js";

// No placeholder OAuth fallbacks (claude review 2026-09-19, MEDIUM-2): the
// previous `|| "test-client"` fallbacks meant a NODE_ENV mishap started the
// app silently with publicly known credentials and no proxy list. Missing
// Google credentials now fail the boot via validateRuntimeEnv (env.ts,
// production-like hardened) — they are required there. Dev/test without
// credentials can still import the module for static contract checks; the
// Google provider is simply never exercised in such runs.
const googleClientId = process.env.GOOGLE_CLIENT_ID;
const googleClientSecret = process.env.GOOGLE_CLIENT_SECRET;

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
  socialProviders: {
    google: {
      // These are only read when the module is actually used; in a production
      // boot without credentials validateRuntimeEnv() has already thrown.
      clientId: googleClientId ?? "test-client",
      clientSecret: googleClientSecret ?? "test-secret",
      requireEmailVerification: true,
      // Google is a trusted provider, but implicit linking is globally disabled;
      // linking happens only via explicit /link-social from an existing session.
      disableImplicitSignUp: true,
    },
  },
  // Local mock-OIDC test path (CI only): overrides the google provider with a
  // generic-OIDC config against TEST_OIDC_ISSUER. Same provider id "google";
  // generic providers are prepended to socialProviders (shadows built-in).
  plugins: process.env.TEST_OIDC_ISSUER ? [
    genericOAuth({
      config: [
        {
          providerId: "google",
          clientId: process.env.TEST_OIDC_CLIENT_ID || "test-client",
          clientSecret: process.env.TEST_OIDC_CLIENT_SECRET || "test-secret",
          // fetchDiscovery fetches the URL directly — full discovery-document
          // URL required, not the bare issuer.
          discoveryUrl: `${process.env.TEST_OIDC_ISSUER}/.well-known/openid-configuration`,
          scopes: ["openid", "email", "profile"],
          requireEmailVerification: true,
          disableImplicitSignUp: true,
        },
      ],
    }),
  ] : [],
  account: {
    accountLinking: {
      enabled: true,
      trustedProviders: ["google"],
      // CONTRACTS-v2: implicit same-email linking is disabled. Same-email Google
      // sign-in before explicit linking must fail (link-account refusal path,
      // dist/oauth2/link-account.mjs:79 — verified in 1.7.5).
      disableImplicitLinking: true,
    },
  },
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
      "/sign-in/social": { window: 60, max: 10 },
      "/link-social": { window: 60, max: 10 },
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