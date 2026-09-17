import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

describe("Contract Verification Tests (react-hono-postgres-v2)", () => {
  it("Vite config base is absolute root (v2: root paths, no StripPrefix)", () => {
    const viteConfig = readFileSync(join(process.cwd(), "vite.config.ts"), "utf-8");
    expect(viteConfig).toMatch(/base:\s*['"]\/['"]/);
  });

  it("Client fetches are absolute origin paths (v2)", () => {
    const appTsx = readFileSync(join(process.cwd(), "source/client/App.tsx"), "utf-8");
    expect(appTsx).toMatch(/fetch\(['"]\/api\//);
    expect(appTsx).not.toMatch(/fetch\(['"]\.\/api\//);
  });

  it("Server routes are mounted at root (Host routing, no StripPrefix)", () => {
    const serverTs = readFileSync(join(process.cwd(), "source/server/server.ts"), "utf-8");
    expect(serverTs).toMatch(/app\.get\(['"]\/healthz['"]/);
    expect(serverTs).toMatch(/app\.get\(['"]\/readyz['"]/);
    expect(serverTs).toMatch(/app\.get\(['"]\/api\/entries['"]/);
    expect(serverTs).toMatch(/app\.get\(['"]\/\*['"]/); // SPA fallback
  });

  it("Liveness endpoint performs no DB query", () => {
    const serverTs = readFileSync(join(process.cwd(), "source/server/server.ts"), "utf-8");
    const healthzMatch = serverTs.match(/app\.get\("\/healthz"[^;]+;/);
    expect(healthzMatch).toBeTruthy();
    expect(healthzMatch![0]).not.toMatch(/db\.execute|select 1/);
  });

  it("Readiness endpoint verifies PostgreSQL", () => {
    const serverTs = readFileSync(join(process.cwd(), "source/server/server.ts"), "utf-8");
    const readyzMatch = serverTs.match(/app\.get\("\/readyz"[\s\S]+?\n\}\);/);
    expect(readyzMatch).toBeTruthy();
    expect(readyzMatch![0]).toMatch(/db\.execute\(sql`select 1`\)/);
  });

  it("Dockerfile healthcheck targets readyz, migration precedes server", () => {
    const dockerfile = readFileSync(join(process.cwd(), "Dockerfile"), "utf-8");
    expect(dockerfile).not.toMatch(/db:generate/);
    expect(dockerfile).toMatch(/readyz/);
    expect(dockerfile).toMatch(/migrate\.js && node.*server\.js/);
  });
});
