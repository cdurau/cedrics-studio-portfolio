# Authorization Policy — `react-hono-postgres-v2-user-app`

Human-readable authorization spec required by `templates/CONTRACTS-v2.md`
(§ `user-app` auth contract → *Data isolation*). It describes what the code
**does today**, not an intended end state — including the three places where
it deliberately or knowingly deviates (§5). Keep it in sync when routes,
tables or policies change; a drifting policy spec is worse than none.

Enforcement runs on two independent layers. Either one alone would be a
single point of failure, so both must hold:

1. **Application layer** — `requireUser()` (`source/server/guard.ts`) resolves
   the session and the app-owned `users.id`, deny-by-default.
2. **Database layer** — PostgreSQL row-level security on business tables,
   evaluated against `app.user_id` and enforced against a runtime role that is
   neither table owner nor `BYPASSRLS`.

---

## 1. Subjects

| Subject | How it is established | Identity used downstream |
|---|---|---|
| **Anonymous** | no valid session cookie | — |
| **Authenticated user** | Better-Auth session cookie (`user-app.session_token`, host-only, `Secure`, `HttpOnly`, `SameSite=Lax`, no cookie cache) **and** an existing row in `users` for that `auth_user_id` | `users.id` (uuid) — never the Better-Auth user id |
| **Admin** | **not an HTTP subject.** Shell access to the container or the host, via `npm run admin:*` | database role, no session |

Two consequences worth stating explicitly. A valid session whose `users`
mapping is missing is rejected with 401 (`guard.ts`), so a Better-Auth user
without an identity mapping has no access at all — the mapping is created by
`databaseHooks.user.create.after`. And there is **no admin role over HTTP**:
no route grants elevated rights, and no header, claim or flag can produce one.
Administrative action requires shell access by design.

## 2. Routes

Deny-by-default is applied by `app.use("/api/*", requireUser)`; the exemption
list inside `requireUser()` is exact-match or `/api/auth/` prefix, so a path
like `/api/authsomething` is **not** exempt.

| Route | Method | Subject | Notes |
|---|---|---|---|
| `/healthz` | GET | anonymous | liveness, no DB, no data |
| `/readyz` | GET | anonymous | readiness, DB ping only, 503 when not ready |
| `/api/auth/**` | GET, POST | anonymous | Better-Auth endpoints; sign-up disabled, stricter rate limits on `/sign-in/email`, `/sign-in/social`, `/link-social` |
| `/api/me` | GET | authenticated | own identity only |
| `/api/entries` | GET | authenticated | own rows only (RLS) |
| `/api/entries` | POST | authenticated | `owner_id` is taken from the session, never from the request body |
| `/*` | GET | anonymous | static client shell and SPA fallback — carries no user data; the login screen must be reachable unauthenticated |

Health endpoints are not routed by the external app router (Health Contract),
so they are reachable only inside the container network.

## 3. Resources and rules

| Resource | Rule | Enforced by |
|---|---|---|
| `entries` | a user may read, insert, update and delete **only** rows where `owner_id` equals their own `users.id` | `ENABLE` + `FORCE ROW LEVEL SECURITY`, policy `entries_owner_policy` (`FOR ALL TO app_runtime`, `USING` and `WITH CHECK` on `owner_id = NULLIF(current_setting('app.user_id', true), '')::uuid` — migration 0004) |
| `users` | identity mapping; not exposed by any route. A user learns only their own `users.id` (via `/api/me`) | application layer only — see §5.1 |
| Better-Auth tables (`user`, `session`, `account`, `verification`, `rate_limit`) | managed exclusively by Better-Auth; no business route reads or writes them | application layer — see §5.2 |

**Every business request runs inside a transaction.** `withUser(userId, fn)`
opens the transaction and sets `set_config('app.user_id', <users.id>, true)`
— the third argument makes it transaction-local, so the value cannot leak to
the next request over a pooled connection. The user id is passed as a bound
parameter, never interpolated into SQL text.

**Fail-closed by construction.** The policy expression is
`owner_id = NULLIF(current_setting('app.user_id', true), '')::uuid`
(migration 0004). Two distinct "no context" states must both fail closed, and
only the `NULLIF` form covers both:

| State | `current_setting('app.user_id', true)` | Without `NULLIF` | With `NULLIF` (0004) |
|---|---|---|---|
| setting never set on this connection | `NULL` | `NULL::uuid` → comparison NULL → 0 rows | same → 0 rows |
| connection reused after an earlier `set_config(…, true)`, read outside a transaction | `''` (empty string, **not** NULL) | `''::uuid` raises `invalid input syntax for type uuid` → **HTTP 500** | `NULL::uuid` → comparison NULL → 0 rows |

The empty-string case is real on a pooled connection: PostgreSQL resets a
transaction-local `set_config` value to `''` rather than to NULL once the
transaction ends. The pre-0004 policy therefore turned a correctly denied
query into a 500 instead of an empty result. In both states no row qualifies —
a missing context yields zero rows, never all rows.

Contract test 6 verifies this against the runtime role by direct SQL, with the
setting, without it, and on a reused connection after an earlier transaction
(the `''` path).

## 4. Database roles

| Role | Rights | Used by |
|---|---|---|
| **owner / migration role** | owns the tables, runs DDL | `migrate.ts` via `MIGRATION_DATABASE_URL` (mandatory; no fallback to the runtime URL) |
| **`app_runtime`** | `USAGE` on `public`; `SELECT, INSERT, UPDATE, DELETE` on tables; sequence usage. **Not** the table owner, **no** `BYPASSRLS` | the server via `DATABASE_URL` |

Because `app_runtime` is not the owner, `FORCE ROW LEVEL SECURITY` applies to
it without exception. Roles and their passwords are provisioned at deploy
time; migrations contain no role literals.

One caveat that must not be misread as a safety net. `FORCE` also subjects
the *table owner* to RLS, and since `entries_owner_policy` is scoped
`TO app_runtime`, a non-superuser owner matches no policy and therefore sees
no `entries` rows — a server accidentally started with the owner URL would
fail closed. **This does not hold if the owner role is a superuser**, because
superusers bypass RLS entirely, `FORCE` included. The integration tests run
with a superuser owner (`POSTGRES_USER` of the throwaway container), so they
do **not** demonstrate this property. Provision the production owner as a
plain owner without `SUPERUSER` and without `BYPASSRLS`, and treat
"the server must use `DATABASE_URL`, never `MIGRATION_DATABASE_URL`"
(`source/server/db/client.ts`) as the actual control.

## 5. Known deviations

These are real gaps, listed so nobody has to rediscover them.

**5.1 `users` has no RLS.** Only `entries` carries row-level security. The
runtime role can read and write every row of the identity mapping. This is
load-bearing: `requireUser()` must look up `users` *before* any `app.user_id`
is set, so a policy on that table would have to exempt exactly that lookup.
Impact today is limited — `users` holds only `id`, `auth_user_id` and
`created_at` — but a SQL-injection finding or a leaked runtime credential
could repoint a mapping. Revisit when the second business table lands.

**5.2 Better-Auth tables have no RLS.** Deliberate, documented in
`drizzle/migrations/0002_rls_and_grants.sql`: session creation during sign-in
runs without an `app.user_id` transaction context, so `FORCE` RLS there would
break sign-in. Business isolation is enforced on business tables.

**5.3 Deny-by-default covers `/api/*`, not everything.** The guard is mounted
on that prefix. Every business route today lives under it, but a future route
outside `/api/` would be unauthenticated, and contract test 7 — which checks
the known routes — would not notice. **Any new business route belongs under
`/api/`.** If that ever changes, mount the guard globally and exempt the
static shell instead.

## 6. Adding a business table

1. `owner_id uuid not null references users(id)` — reference `users`, never a
   Better-Auth table.
2. `ENABLE` **and** `FORCE ROW LEVEL SECURITY`, plus an owner policy scoped
   `TO app_runtime` with both `USING` and `WITH CHECK`. `ENABLE` alone does not
   cover the table owner; `USING` alone does not stop a user from writing a row
   owned by someone else.
3. Access it only through `withUser()`; set `owner_id` from the session.
4. Extend the §3 table here and add an A/B isolation test mirroring contract
   test 6, at API level **and** by direct SQL with the runtime role.

## 7. Portability to Appwrite permissions

The contract requires this model to be portable to Appwrite. It maps as
follows; a later migration remaps the identity table and moves enforcement
from RLS to document permissions:

| Here | Appwrite |
|---|---|
| Better-Auth `user` + app-owned `users` mapping | Appwrite Users; `auth_user_id` becomes the Appwrite user `$id`, and the mapping table largely disappears |
| `entries` with `entries_owner_policy` | a collection with **document-level permissions** set at create time: `read("user:<uid>")`, `update("user:<uid>")`, `delete("user:<uid>")`; collection-level `create("users")` |
| `withUser()` / `app.user_id` | not needed — Appwrite evaluates permissions server-side per request |
| `requireUser()` | Appwrite session plus the `users` role; the app-owned id lookup disappears |
| owner vs `app_runtime` split | Appwrite API key (server) vs client session; the key never reaches the browser |
| `admin:*` CLI | Appwrite Server SDK with an API key, run the same way — shell only, never over HTTP |

The deviations in §5 carry over as real work: document permissions would have
to be set on **every** create path, because there is no equivalent of a single
table-wide policy that catches a forgotten one.
