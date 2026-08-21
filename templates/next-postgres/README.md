# next-postgres

**Profile:** `next-postgres`
**Type:** Next.js (App Router) + TypeScript + Tailwind + Drizzle
**Routing:** Subdomain preferred
**Database:** PostgreSQL

This template provides a production-ready Next.js application using the App Router and Server Actions. It is designed for apps needing SSR, rich interactive features, or dedicated subdomains.

## When to use and when NOT to use
- **Use when:** You need a full-stack Next.js app, Server Actions, complex UI state, or direct database access via Drizzle ORM.
- **Do NOT use when:** You just need a simple static site without a backend, or if the app must run perfectly on a subdirectory path without Next.js path rewriting complexity.

## Dependencies
- **Build-time:** Next.js build tools, TypeScript, Tailwind CSS, PostCSS, Drizzle Kit.
- **Runtime:** Node.js 22 Alpine, Next.js standalone mode, Postgres.js, Drizzle ORM (no Hono).

## Development DX
```bash
# Provision a generic local database (do not map public host ports permanently for real data; this is for dev only)
docker run --name temp-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_USER=postgres -e POSTGRES_DB=postgres -p 127.0.0.1:5432:5432 -d postgres:16-alpine

npm install
npm run dev
```

### Database Migrations
Migrations use a custom committed-SQL runner:
1. `npm run db:generate` - Generates SQL locally. You must review the generated SQL before committing.
2. `npm run db:migrate` - Applies the migrations.
Migrations are immutable. The runner enforces SHA-256 hashes of applied migrations; if a file changes after being applied, the runner will reject it and fail-closed. Do NOT mutate committed migration SQL. The runner will rollback the transaction and fail closed if any error occurs. Note that rolling back Next.js app versions != rolling back the DB schema (always use Expand-Contract patterns for DB schemas).

## Environment & Secrets
Requires `DATABASE_URL` (e.g. `postgres://user:password@host:5432/dbname`) for connecting to the database.

## Routing, Root-Origin Behavior & Authentik
- **Root-origin behavior:** Next.js naturally expects to serve assets, cookies, and redirects from the origin root (e.g., `/`, `/_next`). While a dedicated subdomain is preferred to ensure full compatibility, current Phase 16 infrastructure does not yet provision wildcard DNS or Cloudflare/Funnel subdomains. Testing locally via Tailscale or Docker might only validate root paths.
- **Authentik:** Authentication is deferred to the Traefik ForwardAuth layer. The app consumes trusted headers (e.g., `X-authentik-username`, `X-authentik-email`, `X-authentik-groups`, `X-authentik-uid`, `X-authentik-name`) via `src/lib/auth.ts` server-side helper. No secondary auth system should be built.

## Production & Coolify
- The app builds using Next.js `standalone` mode, producing a minimal container footprint.
- **Isolated shared PG:** The database connection should not use a public port.
- Database migrations run automatically (`node dist/migrate.js`) before the Next.js server boots. If migrations fail, the app fails closed.
- Runs non-root (`app` user).

## Customization Files
When modifying this template, you will typically change:
- `src/app/**` (Pages, Layouts, API routes)
- `src/lib/schema.ts` (Drizzle schema)
- `tailwind.config.ts` and `src/app/globals.css` (Styles)
- `package.json` (Name and metadata)

## CI / CD
Changes in `apps/<slug>/**` trigger automatic builds in Coolify.
The CI requires the following secrets to be configured:
- `COOLIFY_API_TOKEN`
- `COOLIFY_<APP_SLUG>_UUID`
- `DATABASE_URL` (in Coolify environment settings for the app)
