-- 0002_rls_and_grants.sql — RLS + grants for the user-app profile.
-- No role creation, no literals: roles (app_runtime + owner/migration role)
-- are provisioned at deploy/bootstrap time (see README — deploy section).
-- Migrations run with the OWNER role; the server runs as app_runtime.

-- 1) RLS on the business table: owner-only rows for the runtime role.
ALTER TABLE entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE entries FORCE ROW LEVEL SECURITY;

CREATE POLICY entries_owner_policy ON entries
  FOR ALL
  TO app_runtime
  USING (owner_id = current_setting('app.user_id', true)::uuid)
  WITH CHECK (owner_id = current_setting('app.user_id', true)::uuid);

-- 2) Grants for the runtime role. Better-Auth tables and app-owned tables are
--    written by the app (Better Auth adapter + databaseHooks user mapping).
GRANT USAGE ON SCHEMA public TO app_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_runtime;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO app_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO app_runtime;

-- NOTE: No RLS on Better-Auth session/account tables — session creation during
-- sign-in runs without an app.user_id transaction context, so FORCE RLS there
-- would break sign-in. Business isolation (CONTRACTS-v2) is enforced on
-- business tables; the runtime role has no table ownership and no BYPASSRLS.