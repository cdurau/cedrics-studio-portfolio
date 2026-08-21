# python-fastapi

First-class Python full application profile for the Coolify v5 platform.

## When to choose this profile
- **Yes:** You need a full application with persistence (SQLite or PostgreSQL) and UI.
- **Yes:** Python is required for ML/data processing.
- **No:** You only need an API without UI (use `python-service`).
- **No:** You don't need Python specifically (use `react-hono-sqlite` or `react-hono-postgres`).

## Infrastructure & Routing
- **Routing:** Path or Dedicated subdomain.
- **Port:** Internal 3000 (do not publish host ports).
- **Auth:** Rely on Authentik ForwardAuth trusted headers.
- **Database:** SQLite by default (at `/data/app.db`). Configurable to isolated PostgreSQL via `DATABASE_URL` (includes `psycopg` runtime).

## Local Development
1. Create a virtual environment: `python3 -m venv venv`
2. Activate: `source venv/bin/activate`
3. Install dependencies: `pip install -r requirements.txt -r requirements-dev.txt`
4. Run migrations: `alembic upgrade head`
5. Run locally: `uvicorn source.main:app --reload --port 3000`
6. Test: `pytest`

### Generating Migrations
When you change `source/models.py`, generate a new migration:
```bash
alembic revision --autogenerate -m "description of changes"
```
Review the generated file in `alembic/versions/` and commit it. Migrations are executed at startup in the Docker container and will prevent the server from starting if they fail.

SQLite does not provide transactional rollback for every DDL operation. Keep migrations additive, validate them against a disposable database, and use Expand/Contract. A failed deployment prevents the new container from becoming healthy, but application rollback is not database rollback.

## Deployment & CI
- Built and deployed as a Coolify Dockerfile Application via the standard GitHub Actions path filter (`apps/<slug>/**`).
- **Secrets:** Do not commit `.env`. `COOLIFY_API_TOKEN` and `COOLIFY_<APP_SLUG>_UUID` must be set in GitHub repository secrets. Runtime variables are injected via Coolify.
- **Volumes:** For SQLite, a volume mounted at `/data` must be configured in Coolify to ensure persistence across deployments.
