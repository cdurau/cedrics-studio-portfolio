import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import type { VaultConfig } from "../src/config.js";
import { createServer } from "../src/server.js";
import { VaultRegistry } from "../src/vault/vault.js";

export function makeVault(files: Record<string, string>): { root: string; cleanup: () => void } {
  const root = mkdtempSync(path.join(os.tmpdir(), "obsidian-mcp-test-"));
  for (const [rel, content] of Object.entries(files)) {
    const abs = path.join(root, rel);
    mkdirSync(path.dirname(abs), { recursive: true });
    writeFileSync(abs, content);
  }
  return { root, cleanup: () => rmSync(root, { recursive: true, force: true }) };
}

export async function connect(vaults: VaultConfig[], opts: { locale?: string } = {}) {
  const registry = await VaultRegistry.create(vaults, { defaultVault: vaults[0]!.name, locale: opts.locale ?? "de" });
  const server = createServer(registry);
  const [clientT, serverT] = InMemoryTransport.createLinkedPair();
  await server.connect(serverT);
  const client = new Client({ name: "test", version: "0.0.0" });
  await client.connect(clientT);

  async function call(name: string, args: Record<string, unknown> = {}) {
    const res = (await client.callTool({ name, arguments: args })) as {
      isError?: boolean;
      content: { type: string; text: string }[];
    };
    return res;
  }
  async function callJson<T = any>(name: string, args: Record<string, unknown> = {}): Promise<T> {
    const res = await call(name, args);
    if (res.isError) throw new Error(`Tool ${name} failed: ${res.content[0]?.text}`);
    return JSON.parse(res.content[0]!.text) as T;
  }
  async function callText(name: string, args: Record<string, unknown> = {}): Promise<string> {
    const res = await call(name, args);
    if (res.isError) throw new Error(`Tool ${name} failed: ${res.content[0]?.text}`);
    return res.content.map((c) => c.text).join("\n---\n");
  }
  return { client, registry, call, callJson, callText, close: () => client.close() };
}
