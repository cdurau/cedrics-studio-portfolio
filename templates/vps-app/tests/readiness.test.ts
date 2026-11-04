import { afterEach, describe, expect, it, vi } from "vitest";
const sha = "a".repeat(40);
afterEach(() => { vi.unstubAllEnvs(); vi.resetModules(); vi.doUnmock("node:fs"); vi.doUnmock("../source/server/db/client.js"); });
async function load(commit: string, down = false) {
  vi.resetModules();
  vi.doMock("node:fs", () => ({ readFileSync: () => JSON.stringify({ commit }) }));
  vi.doMock("../source/server/db/client.js", () => ({ db: { execute: async () => {
    if (down) throw new Error("unavailable");
  } } }));
  return (await import("../source/server/readiness.js")).readiness;
}
describe("build-bound readiness", () => {
  it("reports immutable build commit and ignores runtime override", async () => {
    vi.stubEnv("NODE_ENV", "production"); vi.stubEnv("APP_COMMIT_SHA", "b".repeat(40));
    expect(await (await load(sha))()).toEqual({ ok: true, commit: sha });
  });
  it.each(["", "bad", "a".repeat(39)])("fails production readiness for invalid commit %s", async commit => {
    vi.stubEnv("NODE_ENV", "production");
    expect(await (await load(commit))()).toEqual({ ok: false, commit: null });
  });
  it("fails readiness when DB fails", async () => {
    expect(await (await load(sha, true))()).toEqual({ ok: false, commit: sha });
  });
});
