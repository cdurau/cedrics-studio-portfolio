# react-hono-postgres

A lightweight, production-capable starting point for single-page React apps backed by Hono, Drizzle ORM, and PostgreSQL.

## 1. When to Use (and When Not To)
**Choose this profile for:**
- Normal multi-device applications requiring relational data and longevity.
- Standard CRUD applications fitting inside a cohesive TypeScript codebase.
- Projects that do not require server-side rendering (SSR).

**Do not choose this if:**
- You do not need a database or shared server state (use `static-vite`).
- You need a small self-hosted app where SQLite suffices (use `react-hono-sqlite`).
- You need AI/ML libraries like Pandas or Python services (use `python-fastapi` or `python-service`).
- You require Server Components or SSR (use `next-postgres`).

## 2. Dependencies & Architecture
- **Frontend**: React 19, Vite, Tailwind CSS
- **Backend**: Hono (running on Node.js 22+)
- **Database**: PostgreSQL (via Drizzle ORM and `pg`)
- **Auth**: Trusted Authentik identity headers via Traefik ForwardAuth (evaluated before `StripPrefix`)

## 3. Installation
1. Ensure Node.js 22+ is installed (and Docker for integration tests).
2. Run `npm ci` to install dependencies cleanly.

## 4. Environment Variables & Secrets
- Copy `.env.example` to `.env`.
- `DATABASE_URL` is required. In local development, use a local connection string (e.g., `postgresql://testuser:testpass@127.0.0.1:5432/testdb`).
- In production, runtime environment variables like `DATABASE_URL` are injected directly via Coolify. No real `.env` file is committed to version control.
- Deployment control-plane secrets: Deployments are triggered via GitHub Actions using the shared repository secret `COOLIFY_API_TOKEN` and the per-app secret `COOLIFY_<APP_SLUG>_UUID`. These are deployment/control-plane secrets, not app runtime secrets. The template repository contains neither.

## 5. Local Development Start
The application requires running the frontend and backend simultaneously:
- Backend: `npm run dev:server` (Starts Hono with auto-reload on port 3000)
- Frontend: `npm run dev:client` (Starts Vite dev server)

## 6. Database & Migrations
- **Generate**: `npm run db:generate` reads your schema in `source/server/db/schema.ts` and creates SQL migration files in `drizzle/migrations/` and updates `meta/_journal.json`. **It does not apply them to the database.**
- **Review & Commit**: Migration SQL files and the journal must be reviewed and committed to version control. Do NOT generate migrations inside the container image or at runtime in production.
- **Fail-Closed Execution**: At startup, `CMD ["sh", "-c", "node dist/server/server/migrate.js && node dist/server/server/server.js"]` executes committed migrations before the server boots. If migrations fail, the container exits immediately before serving traffic.
- **Expand/Contract**: App rollback is not DB rollback. Schema migrations must follow the Expand/Contract pattern to keep database changes backwards- and forwards-compatible across deployments.

## 7. Testing & Build
- **Unit Tests**: `npm run test:unit` (tests auth helpers and contracts with Vitest).
- **Integration Tests**: `npm run test:integration` (requires Docker; automatically provisions, tests against, and tears down an ephemeral `postgres:18-alpine` test container without requiring a pre-existing PostgreSQL server).
- **Build**: `npm run build` compiles both the Vite client (`dist/client`) and TypeScript server (`dist/server`).

## 8. Coolify Dockerfile & Container Settings
- **Base Directory**: `apps/<slug>` within the monorepo.
- **Internal Port**: Listens on port `3000` (`EXPOSE 3000`); no host ports are published.
- **Health Check**: `GET /healthz` returning `{"ok":true}` HTTP 200.
- **Routing Integration**: Path-routed via Traefik `StripPrefix`.
- **Runtime Environment**: Injected dynamically by Coolify at runtime (no baked credentials).

## 9. CI/CD & Pipeline
- **GitHub Actions**: Builds are triggered using a path filter for the app directory: `apps/<slug>/**` (not the template directory).
- **Tailscale WIF**: GitHub Actions authenticates via Tailscale Workload Identity Federation to securely interact with the private environment.
- **No Public Webhooks**: The Coolify Deploy API is triggered internally via the private network using `COOLIFY_API_TOKEN` and `COOLIFY_<APP_SLUG>_UUID`.

## 10. Routing (Path vs Subdomain)
- This template uses **Path Routing** (default). Vite is configured with a relative base (`./`), and all API requests are relative (`./api/...`).
- Traefik `StripPrefix` handles routing, mapping `https://<apps-host>/<slug>/` internally to `/`.
- Dedicated subdomains are reserved for future origin-isolated apps and are not provisioned in Phase 10.

## 11. Authentik Integration
- **ForwardAuth Middleware**: Authentication happens at the Traefik layer via Authentik ForwardAuth *before* `StripPrefix` executes.
- **Frozen Infrastructure**: This ForwardAuth configuration is frozen infrastructure and is not generated or modified by the template.
- **Trusted Identity Headers**: The app reads identity from headers (`X-authentik-username`, `X-authentik-email`, `X-authentik-name`, `X-authentik-uid`, `X-authentik-groups`), which are trusted only behind this proxy contract. No secondary or internal authentication database is used.

## 12. Persistence & Isolation
- **PostgreSQL**: The app connects to an isolated database and dedicated role on the shared `studio-postgres` cluster.
- The database user only has schema-level permissions; migrations must not execute `CREATE DATABASE`.

## 13. Exact Customization File List
When creating a new application from this template into `apps/<slug>`:
1. Update `package.json` (`name`, version, description) and `README.md` to reflect the app identity.
2. Build your UI components and pages in `source/client/**`.
3. Add your routes, endpoints, and Auth helper usages in `source/server/**`.
4. Define your Drizzle database tables in `source/server/db/schema.ts`.
5. Run `npm run db:generate`, review the generated SQL files in `drizzle/migrations/` and `meta/_journal.json`, and commit them to Git.
6. Update `.env.example` with placeholders for any new app-specific environment variables.
7. Review Docker and Coolify configuration settings (base directory `apps/<slug>`, internal port 3000, health check `/healthz`).
8. Create an app-specific deployment workflow (with path filter `apps/<slug>/**`) and configure the `COOLIFY_<APP_SLUG>_UUID` secret in repository settings outside the template.
