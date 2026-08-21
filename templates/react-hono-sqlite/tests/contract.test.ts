import { describe, it, expect } from "vitest";
import { readFileSync, existsSync } from "node:fs";
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

  it("Has no Postgres dependency and uses better-sqlite3", () => {
    const pkgJson = JSON.parse(readFileSync(join(process.cwd(), "package.json"), "utf-8"));
    expect(pkgJson.dependencies?.pg).toBeUndefined();
    expect(pkgJson.dependencies?.['better-sqlite3']).toBeDefined();
  });

  it("Dockerfile sets DB_PATH and chowns /data", () => {
    const dockerfile = readFileSync(join(process.cwd(), "Dockerfile"), "utf-8");
    expect(dockerfile).toMatch(/ENV DB_PATH=\/data\/app\.db/);
    expect(dockerfile).toMatch(/RUN mkdir \/data && chown app:app \/data/);
  });

  it("No Compose file exists in the template", () => {
    expect(existsSync(join(process.cwd(), "docker-compose.yml"))).toBe(false);
    expect(existsSync(join(process.cwd(), "compose.yaml"))).toBe(false);
  });
});
