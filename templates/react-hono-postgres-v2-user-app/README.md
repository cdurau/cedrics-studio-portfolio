# react-hono-postgres-v2-user-app

Studio user-app profile implementing the `user-app` auth contract from `templates/CONTRACTS-v2.md` (Better Auth S0 reference baseline).

## Features

- **Better-Auth 1.7.5** (exact pin, lockfile committed) with Drizzle adapter and explicit schema mapping.
- **Auth methods:** email/password + Google OAuth. **Sign-up disabled** — users are admin-seeded only (`admin:seed-user`), `emailAndPassword.requireEmailVerification` on.
- **Password hashing: Argon2id** via `@node-rs/argon2` (exact pin), parameters `m=19456, t=2, p=1` (RFC 9106 recommended profile). PHC string format `$argon2id$v=19$m=19456,t=2,p=1$...` — import-compatible with Appwrite user import.
- **Account linking:** implicit linking disabled (`account.accountLinking.disableImplicitLinking`), Google `requireEmailVerification: true`. Same-email Google sign-in before explicit linking fails (`account_not_linked`); linking only via `/api/auth/link-social` from an existing session; different-email accounts are never merged.
- **Identity mapping:** app-owned `users(id uuid, auth_user_id unique)` table; creation via Better-Auth `databaseHooks.user.create.after`. Business tables reference only `users.id`.
- **RLS:** example business table `entries(owner_id → users)` with `ENABLE` + `FORCE ROW LEVEL SECURITY`, owner policy on `NULLIF(current_setting('app.user_id', true), '')::uuid` (empty-string-safe, migration 0004). Helper `withUser(userId, fn)` runs transactions with `set_config('app.user_id', …, true)` (parameterized).
- **Two DB roles:** owner/migration role (runs migrations via `MIGRATION_DATABASE_URL`, used by `db:migrate` and the **separate deploy migrate step**) and runtime role `app_runtime` (server; not owner, no `SUPERUSER`/`BYPASSRLS`, no journal access — migration 0003). Roles are provisioned at deploy/bootstrap — **no passwords in migrations**.
- **Session security:** host-only cookies, `Secure`, `HttpOnly`, `SameSite=Lax` (`advanced.defaultCookieAttributes`), prefix `user-app`. **Cookie cache off** — incident revocation is immediate.
- **Rate limiting:** DB storage (survives Blue/Green redeploy), enabled, stricter rules on sign-in/link endpoints. Rule keys are basePath-normalized (no `/api/auth` prefix).
- **Health:** `/healthz` (liveness, no DB), `/readyz` (readiness, DB) — outside the auth gate.
- **PWA shell:** `manifest.webmanifest`, `sw.js` (network-first + offline fallback; `/api/*`, `/healthz`, `/readyz` are never cached), `offline.html` and placeholder icons under `source/client/public/`, copied into `dist/client` via `publicDir`. Regenerate the icons with `node scripts/generate-icons.mjs`; real apps replace them with their own artwork. Installability is asserted by the static contract test.
- **Authorization policy:** [`AUTHORIZATION.md`](./AUTHORIZATION.md) — subjects, routes, per-table rules, the two enforcement layers, known deviations and the Appwrite mapping (required by CONTRACTS-v2 § *Data isolation*).

## Rotation (verified against better-auth 1.7.5, decision 2026-09-19)

`BETTER_AUTH_SECRETS` format: comma-separated `<version>:<secret>`, first entry = current (e.g. `2:<new>,1:<old>`).

**Session semantics:** the session-token cookie is HMAC-signed with the flat first secret only (`dist/context/create-context.mjs` → `secret = secretsArray[0].value`; `dist/cookies/index.mjs` `setSessionCookie`). There is **no session-cookie rollover** in 1.7.5 — the JWT/kid multi-secret path applies only to encrypted JWTs/OAuth state. Planned rotation therefore **invalidates existing sessions** (users re-login; safe default) while new logins keep working. Keep the old version in the list only to decrypt legacy encrypted data; remove it after the bounded window.

**Emergency revocation** is `admin:revoke-sessions <email>` (server-side session deletion), never a secret change.

## Admin CLI (container/mini only)

Interactive commands require a TTY (`docker exec -it …`); piped stdin is refused with exit 1. The password prompt is not echoed and accepts pasted input.

- `npm run admin:seed-user` — creates a verified user; password via hidden stdin prompt only.
- `npm run admin:reset-password` — resets a credential password (does **not** end existing sessions — for a compromised password also run `admin:revoke-sessions` afterwards).
- `npm run admin:revoke-sessions <email>` — incident revoke: deletes all server-side sessions.

## Local development

`npm run dev:server` / `npm run dev:client` set `NODE_ENV=development` themselves — this is required. The boot validation treats **every** `NODE_ENV` other than `development` and `test` as production-like (including an unset one) and then demands `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and `BETTER_AUTH_TRUSTED_PROXIES`, and refuses `TEST_OIDC_ISSUER`. That default is deliberate: it fails closed when Coolify overwrites `NODE_ENV` or drops it. For the same reason `.env.example` carries no `NODE_ENV=development` line — copied into a real `.env`, it would switch the hardening off.

## Tests

- `npm run test:unit` — static contract tests (pins, config assertions, header scan, no-literals migration check, manifest + PWA shell, trusted-proxy client-space guard).
- `npm run test:integration` — all 12 mandatory contract tests from CONTRACTS-v2 against an ephemeral Postgres (`tests/run-integration.sh`), Google simulated via a local mock OIDC provider (`tests/mock-oidc.ts`) incl. `email_verified=false` fixture. The app server runs with the runtime role; migrations/admin ops use the owner role.
- `tests/live/` — the cases an ephemeral suite **cannot** prove, because they depend on this deployment rather than on the library: `ua-smoke-live-auth.sh` (unverified denial, same-email Google before linking), `ua-smoke-live-ab.sh` (A/B isolation through the live API, not just via SQL), `ua-smoke-live-ratelimit.sh` (per-client rate-limit buckets across two real devices), `deploy-gate-negative.sh`, and `xff-probe.md` (how to derive `BETTER_AUTH_TRUSTED_PROXIES` from measurement instead of assumption). All of them print statuses and counts only — never cookies, ids or secrets.

## Deploy notes (S0-13)

- **Slug substitution (bounded).** Validate the slug against
  `^[a-z0-9][a-z0-9-]*$` **first**, then replace every `__APP_SLUG__` token in
  these files with it: `app.yaml`, `index.html`,
  `source/client/public/manifest.webmanifest`, `source/client/public/sw.js`.
  Never choose the origin separately: it is always `<slug>.exampletailnet.ts.net`.
  Refuse generation if a placeholder remains anywhere afterwards. The profile
  carries no concrete app identity (CONTRACTS-v2 § *App Manifest Contract*,
  template identity rule) — a manifest naming a real app is an identity trap.
- Coolify runtime env: `DATABASE_URL` (runtime role), `BETTER_AUTH_SECRETS`, `BETTER_AUTH_URL`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `BETTER_AUTH_TRUSTED_PROXIES` (mandatory in production: comma-separated Traefik CIDR/IP list; without it the IP rate limiter collapses into the global `no-trusted-ip` bucket). Nothing as build arg; `inject_build_args_to_dockerfile=false`.
- **M5 (Gesamtreview 2026-09-20):** the container CMD is server-only — the owner/migration credential (`MIGRATION_DATABASE_URL`) is **not** in the runtime environment. Migrations run as a **separate pre-start step** in the deploy pipeline, before starting/updating the app container. `migrate.ts` retries transient DB-unavailability errors with bounded backoff (5 attempts) and otherwise fails closed; a DB outage at deploy time no longer produces a migrate/restart loop in the app container.
- **Deploy gate (gate review 2026-09-20, WR-01):** that pre-start step is not optional discipline — it is `scripts/deploy-gate.sh`, which runs **on the host** and is the whole deploy: migrate with the owner role → trigger Coolify → poll the deployment UUID to a terminal state → verify a **healthy** container built from **this commit** is serving. A trigger that returns HTTP 200 proves only that the trigger was accepted; this repo already has a deploy whose trigger succeeded and whose container never booted. The workflow template is `ci/deploy-__APP_SLUG__.yml`; GitHub passes the commit SHA and never holds the owner credential. `tests/live/deploy-gate-negative.sh` proves both failure modes turn the gate red, and `VERIFY_ONLY=1 scripts/deploy-gate.sh <sha>` answers "is what is running actually this commit?" without changing anything.
- Bootstrap before first boot: create both DB roles (passwords from owner-only secrets file) — the owner role **without** `SUPERUSER`/`BYPASSRLS` (LOW-3/4/5, Gesamtreview 2026-09-20; it only needs its tables' ownership), the runtime role plain `LOGIN`. Then run the migrate step before the first app boot.
- `app.yaml`: `class: user-app`, `auth.method: better-auth`, `forward_auth: false`.
- Auth code is protected by the **planning repo's** root `CODEOWNERS` (`source/server/auth.ts`, `source/client/auth.ts`). A generated app in the runtime repo has its own ownership layout — do not claim that protection in an app README before the runtime-side path guard and CODEOWNERS actually exist (S0-8/9; gate review IN-04). The contract test skips the CODEOWNERS assertion outside the planning repo, so a copied app never gets false assurance from a green test run.
