# react-hono-postgres-v2-user-app

Studio user-app profile implementing the `user-app` auth contract from `templates/CONTRACTS-v2.md` (Better Auth S0 reference baseline).

## Features

- **Better-Auth 1.7.5** (exact pin, lockfile committed) with Drizzle adapter and explicit schema mapping.
- **Auth methods:** email/password + Google OAuth. **Sign-up disabled** — users are admin-seeded only (`admin:seed-user`), `emailAndPassword.requireEmailVerification` on.
- **Password hashing: Argon2id** via `@node-rs/argon2` (exact pin), parameters `m=19456, t=2, p=1` (RFC 9106 recommended profile). PHC string format `$argon2id$v=19$m=19456,t=2,p=1$...` — import-compatible with Appwrite user import.
- **Account linking:** implicit linking disabled (`account.accountLinking.disableImplicitLinking`), Google `requireEmailVerification: true`. Same-email Google sign-in before explicit linking fails (`account_not_linked`); linking only via `/api/auth/link-social` from an existing session; different-email accounts are never merged.
- **Identity mapping:** app-owned `users(id uuid, auth_user_id unique)` table; creation via Better-Auth `databaseHooks.user.create.after`. Business tables reference only `users.id`.
- **RLS:** example business table `entries(owner_id → users)` with `ENABLE` + `FORCE ROW LEVEL SECURITY`, owner policy on `current_setting('app.user_id', true)`. Helper `withUser(userId, fn)` runs transactions with `set_config('app.user_id', …, true)` (parameterized).
- **Two DB roles:** owner/migration role (runs migrations via `MIGRATION_DATABASE_URL`, used by `db:migrate` and the Dockerfile boot step) and runtime role `app_runtime` (server; not owner, no `BYPASSRLS`). Roles are provisioned at deploy/bootstrap — **no passwords in migrations**.
- **Session security:** host-only cookies, `Secure`, `HttpOnly`, `SameSite=Lax` (`advanced.defaultCookieAttributes`), prefix `user-app`. **Cookie cache off** — incident revocation is immediate.
- **Rate limiting:** DB storage (survives Blue/Green redeploy), enabled, stricter rules on sign-in/link endpoints. Rule keys are basePath-normalized (no `/api/auth` prefix).
- **Health:** `/healthz` (liveness, no DB), `/readyz` (readiness, DB) — outside the auth gate.

## Rotation (verified against better-auth 1.7.5, decision 2026-09-19)

`BETTER_AUTH_SECRETS` format: comma-separated `<version>:<secret>`, first entry = current (e.g. `2:<new>,1:<old>`).

**Session semantics:** the session-token cookie is HMAC-signed with the flat first secret only (`dist/context/create-context.mjs` → `secret = secretsArray[0].value`; `dist/cookies/index.mjs` `setSessionCookie`). There is **no session-cookie rollover** in 1.7.5 — the JWT/kid multi-secret path applies only to encrypted JWTs/OAuth state. Planned rotation therefore **invalidates existing sessions** (users re-login; safe default) while new logins keep working. Keep the old version in the list only to decrypt legacy encrypted data; remove it after the bounded window.

**Emergency revocation** is `admin:revoke-sessions <email>` (server-side session deletion), never a secret change.

## Admin CLI (container/mini only)

- `npm run admin:seed-user` — creates a verified user; password via hidden stdin prompt only.
- `npm run admin:reset-password` — resets a credential password (does **not** end existing sessions — for a compromised password also run `admin:revoke-sessions` afterwards).
- `npm run admin:revoke-sessions <email>` — incident revoke: deletes all server-side sessions.

## Local development

`npm run dev:server` / `npm run dev:client` set `NODE_ENV=development` themselves — this is required. The boot validation treats **every** `NODE_ENV` other than `development` and `test` as production-like (including an unset one) and then demands `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and `BETTER_AUTH_TRUSTED_PROXIES`, and refuses `TEST_OIDC_ISSUER`. That default is deliberate: it fails closed when Coolify overwrites `NODE_ENV` or drops it. For the same reason `.env.example` carries no `NODE_ENV=development` line — copied into a real `.env`, it would switch the hardening off.

## Tests

- `npm run test:unit` — static contract tests (pins, config assertions, header scan, no-literals migration check).
- `npm run test:integration` — all 12 mandatory contract tests from CONTRACTS-v2 against an ephemeral Postgres (`tests/run-integration.sh`), Google simulated via a local mock OIDC provider (`tests/mock-oidc.ts`) incl. `email_verified=false` fixture. The app server runs with the runtime role; migrations/admin ops use the owner role.

## Deploy notes (S0-13)

- Coolify runtime env: `DATABASE_URL` (runtime role), `MIGRATION_DATABASE_URL` (owner role), `BETTER_AUTH_SECRETS`, `BETTER_AUTH_URL`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `BETTER_AUTH_TRUSTED_PROXIES` (mandatory in production: comma-separated Traefik CIDR/IP list; without it the IP rate limiter collapses into the global `no-trusted-ip` bucket). Nothing as build arg; `inject_build_args_to_dockerfile=false`.
- Bootstrap before first boot: create both DB roles (passwords from owner-only secrets file), then migrations run automatically at container start (owner role).
- `app.yaml`: `class: user-app`, `auth.method: better-auth`, `forward_auth: false`.
- Auth code is protected: `source/server/auth.ts`, `source/client/auth.ts` (see root `CODEOWNERS`).