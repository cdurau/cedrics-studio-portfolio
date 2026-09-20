-- 0003_revoke_journal_from_runtime.sql (claude gesamtreview 2026-09-20, M4)
-- 0002 granted DML "ON ALL TABLES IN SCHEMA public" to app_runtime. That also
-- covered the migration journal public.__drizzle_migrations, which the
-- migrator creates before the first migration. A compromised app process
-- could then rewrite or delete journal rows and push the next boot into a
-- hash-mismatch / re-apply crash loop. The runtime role never needs the
-- journal; only the owner/migration role reads and writes it.
REVOKE ALL ON TABLE public.__drizzle_migrations FROM app_runtime;
REVOKE ALL ON SEQUENCE public.__drizzle_migrations_id_seq FROM app_runtime;
