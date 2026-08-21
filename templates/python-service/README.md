# python-service

Minimal FastAPI/Starlette-style API profile for the Coolify v5 platform.

## When to choose this profile
- **Yes:** You need an internal API, automation service, or model/data endpoint without a UI.
- **Yes:** Python is required for ML/data processing.
- **No:** You need a full web application with a UI and database (use `python-fastapi`).
- **No:** You don't need Python specifically (use a TS profile like `react-hono-sqlite`).
- **No:** You need a database by default (this profile is stateless; add DB only if requirements justify it).

## Infrastructure & Routing
- **Routing:** Dedicated subdomain or API host preferred.
- **Port:** Internal 3000 (do not publish host ports).
- **Auth:** Rely on Authentik ForwardAuth trusted headers (see `get_user_from_headers`).

## Local Development
1. Create a virtual environment: `python3 -m venv venv`
2. Activate: `source venv/bin/activate`
3. Install dependencies: `pip install -r requirements.txt -r requirements-dev.txt`
4. Run locally: `uvicorn source.main:app --reload --port 3000`
5. Test: `pytest`

## Dependencies
- `fastapi`, `uvicorn`: API framework and ASGI server.
- No database/migration dependencies by default.

## Deployment & CI
- The application is built and deployed as a Coolify Dockerfile Application via the standard GitHub Actions path filter (`apps/<slug>/**`).
- **Secrets:** Do not commit `.env`. `COOLIFY_API_TOKEN` and `COOLIFY_<APP_SLUG>_UUID` must be set in GitHub repository secrets, not in the app files. Runtime variables are injected via Coolify.
