import { it, expect } from "vitest";
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
it("isolated runner propagates test failure and removes disposable DB", () => {
  const dir = mkdtempSync(join(tmpdir(), "vps-runner-test-"));
  try {
    const log = join(dir, "calls");
    writeFileSync(join(dir, "docker"), `#!/bin/sh\nprintf '%s\\n' "$*" >> "$CALL_LOG"\ncase "$*" in *' port '*) echo '127.0.0.1:54321';; esac\n`, { mode: 0o700 });
    writeFileSync(join(dir, "npx"), "#!/bin/sh\nexit 23\n", { mode: 0o700 });
    const run = spawnSync("bash", ["tests/run-integration.sh"], { env: { ...process.env, PATH: `${dir}:${process.env.PATH}`, CALL_LOG: log }, encoding: "utf8" });
    expect(run.status).toBe(23);
    const calls = readFileSync(log, "utf8");
    expect(calls).toContain("-p 127.0.0.1::5432");
    expect(calls).toMatch(/rm -fv app-template-test-[a-f0-9]+/);
    if (process.platform === "darwin") expect(calls.split("\n").filter(Boolean).every(line => line.startsWith("--context devbox "))).toBe(true);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
