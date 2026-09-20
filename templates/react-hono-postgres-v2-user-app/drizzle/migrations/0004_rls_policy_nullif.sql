-- 0004_rls_policy_nullif.sql (claude gesamtreview 2026-09-20, LOW-1)
-- The 0002 owner policy cast `current_setting('app.user_id', true)::uuid`
-- directly. On a reused pool connection after an earlier set_config(…, true),
-- current_setting returns '' (not NULL) outside a user transaction, so the
-- cast raises "invalid input syntax for type uuid" — a 500 instead of an
-- empty result. NULLIF converts '' to NULL first: fail-closed (no rows)
-- without the spurious cast error.
DROP POLICY IF EXISTS entries_owner_policy ON entries;
CREATE POLICY entries_owner_policy ON entries
  FOR ALL
  TO app_runtime
  USING (owner_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
  WITH CHECK (owner_id = NULLIF(current_setting('app.user_id', true), '')::uuid);