import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";
import { VaultError } from "../vault/paths.js";
import type { VaultRegistry } from "../vault/vault.js";

export function text(value: string): CallToolResult {
  return { content: [{ type: "text", text: value }] };
}

export function json(value: unknown): CallToolResult {
  return { content: [{ type: "text", text: JSON.stringify(value, null, 2) }] };
}

export function errorResult(e: unknown): CallToolResult {
  const msg =
    e instanceof VaultError
      ? `[${e.code}] ${e.message}`
      : e instanceof Error
        ? e.message
        : String(e);
  return { isError: true, content: [{ type: "text", text: msg }] };
}

/** Fängt Fehler ab und gibt sie als MCP-Tool-Fehler zurück (statt den Server zu crashen). */
export function safe<A>(fn: (args: A) => Promise<CallToolResult>): (args: A) => Promise<CallToolResult> {
  return async (args: A) => {
    try {
      return await fn(args);
    } catch (e) {
      return errorResult(e);
    }
  };
}

export function vaultParam(registry: VaultRegistry) {
  const names = registry.names();
  return z
    .string()
    .optional()
    .describe(
      names.length > 1
        ? `Vault-Name. Standard: "${registry.defaultName}". Verfügbar: ${names.join(", ")}`
        : `Vault-Name (optional, nur "${registry.defaultName}" konfiguriert)`,
    );
}

export function iso(ms: number): string {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
}

export function truncate(s: string, max: number): string {
  return s.length > max ? `${s.slice(0, max)}…` : s;
}

/** Filtert auf einen Ordner (inkl. Unterordner). "" = gesamter Vault. */
export function inFolder(filePath: string, folder: string | undefined): boolean {
  if (!folder) return true;
  const f = folder.replace(/^\/+|\/+$/g, "");
  if (!f) return true;
  return filePath === f || filePath.startsWith(`${f}/`);
}

export function getPath(obj: unknown, dotted: string): unknown {
  if (obj && typeof obj === "object" && dotted in (obj as Record<string, unknown>)) {
    return (obj as Record<string, unknown>)[dotted];
  }
  let cur: unknown = obj;
  for (const part of dotted.split(".")) {
    if (cur === null || typeof cur !== "object") return undefined;
    cur = (cur as Record<string, unknown>)[part];
  }
  return cur;
}
