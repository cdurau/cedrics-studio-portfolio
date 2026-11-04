# App development contract

Develop features in `source/client/App.tsx`, `source/server/routes.ts`,
`source/server/db/schema.ts` and explicit feature modules.

Platform scaffolding (`AuthShell.tsx`, `server.ts`, `auth.ts`, `auth-schema.ts`,
`guard.ts`, `env.ts`, `bootstrap.ts`, `readiness.ts`, `migrate.ts`, entrypoint,
Dockerfile and CI) needs platform review. Preserve closed registration,
verified admin, Argon2id, database sessions/rate limits and deny-by-default APIs.

- Every business table needs ENABLE + FORCE ROW LEVEL SECURITY and an ALL
  policy for app_runtime with USING and WITH CHECK. Generate and commit an
  expand-only migration; never edit an applied migration.
- Resolve identity from the session through requireUser. Business queries
  run inside withUser, setting transaction-local app.user_id. Never use
  migration credentials in business code, and never accept user IDs as authority.
- Auth tables and the app identity mapping are explicit catalog-test exclusions.
  Do not add business tables to the exclusion list.
- Run `npm ci` then `npm run check`. Integration tests create only disposable
  databases; Mac uses `docker --context devbox`. Never use a production DB.
- Keep credentials out of Git, logs, Docker build args and client bundles.
  Only APP_COMMIT_SHA is build metadata. `.env.example` is placeholders only.
- `/readyz` and `/healthz` are internal, excluded at public ingress.
  `/api/deployment` is session-protected and must remain no-store.
- Deploy only through Studio's `studio-app create/publish` once the platform
  gate permits it. Do not add SSH, Coolify tokens, DNS writes, webhooks or
  production deployment steps to CI. Rollback never reverses DB migrations.
