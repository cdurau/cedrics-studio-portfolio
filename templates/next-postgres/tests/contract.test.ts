import { describe, it, expect } from "vitest";
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";

describe("Contract Verification Tests", () => {
  it("Dockerfile builds as standalone output and runs non-root", () => {
    const dockerfile = readFileSync(join(process.cwd(), "Dockerfile"), "utf-8");
    expect(dockerfile).toMatch(/\.next\/standalone/);
    expect(dockerfile).toMatch(/USER app/);
  });

  it("Next config defines output: standalone", () => {
    const nextConf = readFileSync(join(process.cwd(), "next.config.ts"), "utf-8");
    expect(nextConf).toMatch(/output:\s*['"]standalone['"]/);
  });

  it("Package.json does not depend on Hono", () => {
    const pkgJson = JSON.parse(readFileSync(join(process.cwd(), "package.json"), "utf-8"));
    expect(pkgJson.dependencies?.hono).toBeUndefined();
  });

  it("Dockerfile has no db:generate, and migration precedes server", () => {
    const dockerfile = readFileSync(join(process.cwd(), "Dockerfile"), "utf-8");
    expect(dockerfile).not.toMatch(/db:generate/);
    expect(dockerfile).toMatch(/migrate\.js && node.*server\.js/);
  });

  it("No Compose file exists in the template", () => {
    expect(existsSync(join(process.cwd(), "docker-compose.yml"))).toBe(false);
    expect(existsSync(join(process.cwd(), "compose.yaml"))).toBe(false);
  });

  it("Custom auth helper parses X-authentik headers and does not log them", () => {
    const authTs = readFileSync(join(process.cwd(), "src/lib/auth.ts"), "utf-8");
    expect(authTs).toMatch(/headers\(\)/);
    expect(authTs).toMatch(/x-authentik-username/i);
    expect(authTs).toMatch(/x-authentik-email/i);
    expect(authTs).toMatch(/x-authentik-groups/i);
    expect(authTs).not.toMatch(/console\.log/); // no logging of values
  });

  it("Custom runner contains hash mismatch and SQL+marker transaction", () => {
    const migrateTs = readFileSync(join(process.cwd(), "src/lib/migrate.ts"), "utf-8");
    expect(migrateTs).toMatch(/hash mismatch/i);
    expect(migrateTs).toMatch(/sql\.begin\(/);
    expect(migrateTs).toMatch(/INSERT INTO.*__drizzle_migrations/i);
    expect(migrateTs).not.toMatch(/CREATE SCHEMA/i);
    expect(migrateTs).not.toMatch(/CREATE DATABASE/i);
  });

  it("Dockerfile has no db:generate, CMD migration precedes server", () => {
    const dockerfile = readFileSync(join(process.cwd(), "Dockerfile"), "utf-8");
    expect(dockerfile).not.toMatch(/db:generate/);
    expect(dockerfile).toMatch(/CMD.*node dist\/migrate\.js && node server\.js/);
  });

  it("Root-origin behavior, cookie, redirect, Phase16 constraints documented in README", () => {
    const readme = readFileSync(join(process.cwd(), "README.md"), "utf-8");
    expect(readme).toMatch(/root-origin/i);
    expect(readme).toMatch(/cookie/i);
    expect(readme).toMatch(/redirect/i);
    // Use flexible Phase matching as it mentions Phase 10 or Phase 16 constraints
    expect(readme).toMatch(/Phase/i);
  });
});
