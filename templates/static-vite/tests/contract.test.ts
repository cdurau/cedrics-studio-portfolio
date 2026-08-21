import { describe, it, expect } from "vitest";
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";

describe("Contract Verification Tests", () => {
  it("Vite config base is relative", () => {
    const viteConfig = readFileSync(join(process.cwd(), "vite.config.ts"), "utf-8");
    expect(viteConfig).toMatch(/base:\s*['"]\.\/['"]/);
  });

  it("source/build browser-visible URLs not root-absolute", () => {
    const indexHtml = readFileSync(join(process.cwd(), "source/index.html"), "utf-8");
    expect(indexHtml).not.toMatch(/(href|src)=['"]\/[^.]/); // no absolute paths like "/style.css"
    expect(indexHtml).toMatch(/(href|src)=['"]\.\//); // expect relative paths like "./style.css"
  });

  it("Dockerfile uses non-root static server and copies custom config", () => {
    const dockerfile = readFileSync(join(process.cwd(), "Dockerfile"), "utf-8");
    expect(dockerfile).toMatch(/nginxinc\/nginx-unprivileged/);
    expect(dockerfile).toMatch(/COPY nginx\/default.conf/);
    expect(dockerfile).not.toMatch(/VOLUME/); // no mount directives
  });

  it("Nginx config defines healthz before SPA fallback and avoids swallowing static asset 404s", () => {
    const nginxConf = readFileSync(join(process.cwd(), "nginx/default.conf"), "utf-8");
    expect(nginxConf).toMatch(/location = \/healthz/);
    expect(nginxConf).toMatch(/return 200 '{"ok":true}';/);

    // ensure healthz is defined before location /
    const healthzIndex = nginxConf.indexOf('location = /healthz');
    const rootIndex = nginxConf.indexOf('location / {');
    expect(healthzIndex).toBeLessThan(rootIndex);

    // ensure API and assets are not swallowed
    expect(nginxConf).toMatch(/location \/api\//);
    expect(nginxConf).toMatch(/try_files \$uri =404;/);
  });

  it("Has no React, Hono, backend, or DB dependency", () => {
    const pkgJson = JSON.parse(readFileSync(join(process.cwd(), "package.json"), "utf-8"));
    const deps = { ...pkgJson.dependencies, ...pkgJson.devDependencies };

    expect(deps.react).toBeUndefined();
    expect(deps.hono).toBeUndefined();
    expect(deps.express).toBeUndefined();
    expect(deps['drizzle-orm']).toBeUndefined();
    expect(deps.pg).toBeUndefined();
    expect(deps.postgres).toBeUndefined();
    expect(deps.mysql2).toBeUndefined();
  });

  it("No Compose file or socket/mount directives exist in the template", () => {
    expect(existsSync(join(process.cwd(), "docker-compose.yml"))).toBe(false);
    expect(existsSync(join(process.cwd(), "compose.yaml"))).toBe(false);
  });
});
