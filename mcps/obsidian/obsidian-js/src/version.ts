import { readFileSync } from "node:fs";

// Wird beim Bündeln von esbuild ersetzt (siehe scripts/bundle.mjs).
declare const __VERSION__: string | undefined;

function readVersion(): string {
  try {
    const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")) as { version?: string };
    return pkg.version ?? "0.0.0";
  } catch {
    return "0.0.0";
  }
}

export const VERSION = typeof __VERSION__ !== "undefined" ? __VERSION__ : readVersion();
