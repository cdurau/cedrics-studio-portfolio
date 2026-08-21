import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

describe("Contract Verification Tests", () => {
  it("Vite config base is relative", () => {
    const viteConfig = readFileSync(join(process.cwd(), "vite.config.ts"), "utf-8");
    expect(viteConfig).toMatch(/base:\s*['"]\.\/['"]/);
  });

  it("Client fetches are relative", () => {
    const appTsx = readFileSync(join(process.cwd(), "source/client/App.tsx"), "utf-8");
    expect(appTsx).toMatch(/fetch\(['"]\.\/api\//);
    expect(appTsx).not.toMatch(/fetch\(['"]\/api\//);
  });

  it("Server routes are mounted at root post-StripPrefix", () => {
    const serverTs = readFileSync(join(process.cwd(), "source/server/server.ts"), "utf-8");
    expect(serverTs).toMatch(/app\.get\(['"]\/healthz['"]/);
    expect(serverTs).toMatch(/app\.get\(['"]\/api\/entries['"]/);
    expect(serverTs).toMatch(/app\.get\(['"]\/\*['"]/); // SPA fallback
  });

  it("Dockerfile has no db:generate, and migration precedes server", () => {
    const dockerfile = readFileSync(join(process.cwd(), "Dockerfile"), "utf-8");
    expect(dockerfile).not.toMatch(/db:generate/);
    expect(dockerfile).toMatch(/migrate\.js && node.*server\.js/);
  });
});
// Practical Smoke Test Procedure (Documented for CI/CD or Manual verification):
// 1. Build image: docker build -t phase10-react-hono-postgres:final .
// 2. Run ephemeral DB.
// 3. Run app container connected to DB on private network.
// 4. Verify GET /healthz returns 200 {"ok":true}.
// 5. Verify POST /api/entries and GET /api/entries.
// 6. Verify SPA nested route reload (e.g. GET /some/nested/path returns index.html).
