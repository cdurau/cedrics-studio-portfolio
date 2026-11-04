# VPS app template

React + Hono + PostgreSQL + Better Auth 1.7.5, derived from the frozen
`react-hono-postgres-v2-user-app` baseline. Email/password login only; public
registration is disabled. Replace `__APP_SLUG__` with a validated Studio slug.

## Development and checks

Use Node 22 or newer. `npm ci` installs the unchanged baseline lockfile.
`npm run check` runs types, unit tests, isolated DB integration tests and build
in order, stopping at the first failure. On Mac, Docker must have the `devbox`
context. Test scripts bind only disposable PostgreSQL to a random loopback
port and remove it on exit. `npm run test:image` builds a disposable image
and verifies bootstrap, readiness and server environment with a test DB.

Feature files: `source/client/App.tsx`, `source/server/routes.ts`,
`source/server/db/schema.ts`. Platform/auth files are documented in AGENTS.md.
The integration suite retains S0-12 cases 1,2,6–12, checks journal privilege
separation and all business-table RLS policies, and exercises A/B behavior.
Google/OIDC-only cases are intentionally absent.

## Container contract

Build with `docker build --build-arg APP_COMMIT_SHA=<full-40-character-sha> .`
(on Mac explicitly select `--context devbox`). Only the commit is build input;
all credentials are runtime-only. The image is linux/amd64 in CI and runs as
non-root `app`. Do not pass bootstrap secrets to npm build.

The broker injects DATABASE_URL (app_runtime, 64-hex password),
MIGRATION_DATABASE_URL (postgres, same endpoint/database), plural
BETTER_AUTH_SECRETS, HTTPS BETTER_AUTH_URL, measured trusted proxy hops,
ADMIN_EMAIL and ADMIN_PASSWORD. See `.env.example` for placeholders.
Bootstrap takes a bounded advisory lock, creates or validates app_runtime,
authenticates with its exact password, runs the committed migration journal,
and atomically seeds one verified admin and identity mapping only if `user`
is empty. It never rotates an existing role/password/admin. Unexpected role
attributes or password mismatch fail closed. Bootstrap closes its connections.
The entrypoint unsets owner/admin variables before exec of the server.
Docker/Coolify's container configuration still contains those secrets; this
is process-environment reduction, not protection from host root.

Internal `/healthz` is DB-free; `/readyz` returns `{ok,commit}` and 503 for DB
failure or missing/invalid build metadata. The same probe behind requireUser
serves `/api/deployment` with no-store. Anonymous requests get 401. Public
broker ingress must exclude both health paths; this template grants no public
health exception. Commit metadata is read from the built file, not runtime env.

## CI and publishing

`.github/workflows/ci.yml` checks PRs and main pushes. Main pushes build/push
`ghcr.io/example-owner/__APP_SLUG__:<full-sha>` for linux/amd64 using only GITHUB_TOKEN.
Existing SHA tags are refused; the pushed digest is recorded as an artifact.
There is no production deployment from CI. Publishing to a live environment
is a separate reviewed step and is not part of this template. Incompatible schema changes require a separate process.
