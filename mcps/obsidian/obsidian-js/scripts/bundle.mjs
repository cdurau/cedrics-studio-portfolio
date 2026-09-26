// Bündelt den Server in EINE Datei (dist/obsidian-vault-mcp.mjs) – läuft ohne node_modules.
import { build } from "esbuild";
import { chmodSync, readFileSync } from "node:fs";

const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
const outfile = "dist/obsidian-vault-mcp.mjs";

await build({
  entryPoints: ["src/index.ts"],
  outfile,
  bundle: true,
  platform: "node",
  format: "esm",
  target: "node20",
  minify: false,
  sourcemap: false,
  legalComments: "none",
  define: { __VERSION__: JSON.stringify(pkg.version) },
  banner: {
    js: [
      "import { createRequire as __cr } from 'node:module';",
      "const require = __cr(import.meta.url);",
    ].join("\n"),
  },
  logLevel: "info",
});
chmodSync(outfile, 0o755);
