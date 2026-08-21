# static-vite

**Profile:** `static-vite`
**Type:** TypeScript + Vite + Tailwind (No React)
**Routing:** Path routing compatible

This is a minimal static Single Page Application profile. It uses Vite for bundling, TypeScript for type safety, and Tailwind CSS for styling. It runs on a lightweight unprivileged Nginx server in production. React is optional but absent by default to keep the runtime minimal.

## When to use and when NOT to use
- **Use when:** You need a simple, fast, browser-only tool or utility that does not require a backend, a database, or backend authentication logic.
- **Do NOT use when:** You need to store data persistently across devices (no backend database), perform server-side processing, or use backend API routes.

## Dependencies
- **Build-time:** Vite, TypeScript, Tailwind CSS, PostCSS, Autoprefixer, Vitest.
- **Runtime:** Nginx unprivileged Alpine (no Node.js, no Hono, no databases).

## Commands
```bash
# Install dependencies
npm ci

# Local development
npm run dev

# Type checking
npm run typecheck

# Run unit tests
npm run test

# Build for production
npm run build
```

## Environment & Secrets
- **No environment variables or runtime secrets** are needed or supported. The application is completely static.

## Routing, Authentik & Path Behavior
- **Path-safe:** Uses relative base path (`./`) in Vite and avoids absolute `/` paths in HTML.
- **Coolify / Caddy:** The app is deployed under `apps/<slug>` (e.g. `https://studio.local/apps/my-app/`). Traefik/Caddy `StripPrefix` is supported automatically; the app does not need to know its base path.
- **Authentik:** External ForwardAuth protects the route. The app relies completely on the platform's external gateway for security. As a purely static app, it **does not consume trusted headers** (like `X-authentik-username`) because there is no backend server to read them.
- **Health check:** A fast `/healthz` endpoint returning `{"ok":true}` is provided at the root by Nginx before the SPA fallback.

## State & Persistence
- **Device Locality:** Any data is stored locally on the device (e.g., in `localStorage` or `IndexedDB`). It will not synchronize across devices.

## Production & Coolify
- Deploys as a Dockerfile application without Compose or socket/mount directives.
- Exposes internal port `3000`.
- Kept minimal and runs as a non-root user.

## Customization Files
When modifying this template for a new application, you will typically change:
- `source/index.html` (Markup)
- `source/main.ts` (Application logic)
- `source/style.css` (Styles)
- `package.json` (Name and metadata)

## CI / CD
Changes in `apps/<slug>/**` trigger automatic builds in Coolify.
The CI requires the following secrets to be configured:
- `COOLIFY_API_TOKEN`
- `COOLIFY_<APP_SLUG>_UUID`

*Note: No Phase-16 work is included or planned in this template.*
