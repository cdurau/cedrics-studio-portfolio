# react-hono-sqlite (Phase 10 Profile)

A production-capable starting profile for self-hosted apps utilizing **TypeScript, React, Vite, Tailwind, Hono, Drizzle, and SQLite**.

## When to choose this profile
- **Choose SQLite** for small to medium self-hosted apps, personal utilities, low-concurrency data, or apps where data locality and simplicity beat scaling.
- **Do NOT choose SQLite** if your app has high concurrent writes, requires complex relational scaling/reporting beyond a single node, or demands separate database replication (use `react-hono-postgres`).
- **Do NOT choose SQLite** if your app has no server-side state (use `static-vite`).

## Features & Contracts
- **Server-side SQLite:** Database is saved to a persistent volume file rather than an external database server. The runtime image enforces `ENV DB_PATH=/data/app.db`.
- **Coolify Integration:** Containerized deployment using Coolify's Dockerfile application type. The image exposes internal port 3000 (no host port mapping) and serves a `GET /healthz` endpoint returning `200 {"ok":true}` for the orchestration platform. It uses a managed volume mapped to `/data` to persist the database across container replacements.
- **SQLite Optimization:** Write-Ahead Logging (`journal_mode=WAL`), strict foreign keys (`foreign_keys=ON`), and a 5000ms busy timeout (`busy_timeout=5000`) are explicitly enabled on connection to ensure concurrent read safety, data integrity, and mitigation against `SQLITE_BUSY` errors.
- **SQLite Limitations:** Understand SQLite's low-write-concurrency limits. While WAL mode allows multiple readers alongside one writer, concurrent writes are serialized.
- **Authentik Identity:** Expects trusted Traefik/Authentik identity headers (`X-authentik-*`). No built-in password or session handling to reinvent.
- **Safe Migrations:** Commits exact, reviewed SQL. Migrations run transactionally before the Hono server starts (fail-closed, hash-verified, idempotent). No dynamic SQL generation in Docker.
- **Path Routing:** Built for generic `https://<apps-host>/<slug>/` routing. Upstream traffic arrives with the `/slug` stripped via Traefik `StripPrefix`. All Vite assets, API requests, and SPA nested routes utilize base path safety and relative fetches.

## Local Development DX

### Prerequisites
- Node.js 22+
- Docker (for integration testing)

### Installation
```bash
npm ci
```

### Development Server
Run the full-stack environment locally:
1. Start the backend: `npm run dev:server` (Starts Hono API on `localhost:3000`)
2. Start the frontend: `npm run dev:client` (Starts Vite proxying to the backend)

The local SQLite file defaults to `local.db` in the project root. This is excluded from source control.

### Migrations
1. **Generate:** Make schema changes in `source/server/db/schema.ts`, then run `npm run db:generate`.
2. **Review:** Inspect the generated SQL in `drizzle/migrations/`.
3. **Commit:** Commit the new `.sql` file and updated `meta/_journal.json`.
4. **Apply locally:** `npm run db:migrate`. (In production, the Docker startup script handles this.)

### Testing
- `npm run typecheck`
- `npm run test:unit`
- `npm run test:integration` (Executes integration verification and safely runs container replacements validating `/data` volume persistence).

## Customization List
When generating a new app from this template, update:
- `package.json` (`name`)
- Database Schema and related types in `source/server/db/schema.ts`
- API routes in `source/server/server.ts`
- React components in `source/client/`
- Add any required environment variables to `.env.example`

## Production & Coolify Deployment

This profile is configured as a standard single-container **Coolify Dockerfile Application**.

1. **Volume:** Configure a Persistent Volume in Coolify mapped to `/data`. The container non-root user prepares its own permissions.
2. **Environment:**
   - No `DATABASE_URL` or database secrets are required.
3. **No Compose Needed:** The app runs as a standalone Node.js process using a `Dockerfile`.
4. **Backup Contract:** You are responsible for backing up the `/data` volume. **CRITICAL:** Do not use blind, raw file copy (e.g. `cp app.db backup.db`) while the server is running. This creates an inconsistent state. Use the SQLite backup API/command (e.g., `sqlite3 app.db ".backup backup.db"`) or perform a coordinated stop, checkpoint, and snapshot.

### CI Contract
This template expects a CI/CD environment with path filtering (`apps/<slug>/**`). It uses repository secrets (`COOLIFY_<APP_SLUG>_UUID` and `COOLIFY_API_TOKEN`) to trigger zero-downtime updates on the private network.
