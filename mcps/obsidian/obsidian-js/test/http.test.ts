import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import path from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { startHttpServer, type StartedHttpServer } from "../src/http.js";
import { VaultRegistry } from "../src/vault/vault.js";
import { makeVault } from "./helpers.js";

describe("HTTP-Transport", () => {
  const vault = makeVault({ "Hallo.md": "# Hallo\nWelt #gruss\n" });
  let http: StartedHttpServer;
  let base: string;

  beforeAll(async () => {
    const registry = await VaultRegistry.create([{ name: "web", path: vault.root, readOnly: false }], { defaultVault: "web", locale: "de" });
    http = await startHttpServer(registry, { host: "127.0.0.1", port: 0, path: "/mcp", authToken: "geheim", allowNoAuth: false }, () => {});
    base = http.url.replace(/\/mcp$/, "");
  });
  afterAll(async () => {
    await http.close();
    vault.cleanup();
  });

  it("/healthz ohne Auth", async () => {
    const res = await fetch(`${base}/healthz`);
    expect(res.status).toBe(200);
    expect(await res.json()).toMatchObject({ status: "ok", vaults: 1 });
  });

  it("401 ohne bzw. mit falschem Token", async () => {
    const body = JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list" });
    const headers = { "content-type": "application/json", accept: "application/json, text/event-stream" };
    expect((await fetch(`${base}/mcp`, { method: "POST", body, headers })).status).toBe(401);
    expect((await fetch(`${base}/mcp`, { method: "POST", body, headers: { ...headers, authorization: "Bearer falsch" } })).status).toBe(401);
    expect((await fetch(`${base}/andere`)).status).toBe(404);
  });

  it("vollständige MCP-Session über Streamable HTTP", async () => {
    const client = new Client({ name: "http-test", version: "0" });
    await client.connect(
      new StreamableHTTPClientTransport(new URL(http.url), { requestInit: { headers: { authorization: "Bearer geheim" } } }),
    );
    const { tools } = await client.listTools();
    expect(tools.length).toBeGreaterThan(15);
    const res = (await client.callTool({ name: "search_by_tag", arguments: { tags: ["gruss"] } })) as { content: { text: string }[] };
    expect(JSON.parse(res.content[0]!.text).results[0].path).toBe("Hallo.md");
    await client.close();
  });

  it("verweigert öffentliches Binding ohne Token", async () => {
    const registry = await VaultRegistry.create([{ name: "x", path: vault.root, readOnly: true }], { defaultVault: "x", locale: "de" });
    await expect(startHttpServer(registry, { host: "0.0.0.0", port: 0, path: "/mcp", allowNoAuth: false }, () => {})).rejects.toThrow(/MCP_AUTH_TOKEN/);
  });
});

describe("stdio-Transport (gebautes Binary über tsx)", () => {
  it("startet und beantwortet tools/list", async () => {
    const vault = makeVault({ "Eins.md": "eins" });
    const transport = new StdioClientTransport({
      command: process.execPath,
      args: ["--import", "tsx", path.resolve("src/index.ts"), `stdio-test=${vault.root}`],
      stderr: "pipe",
    });
    const client = new Client({ name: "stdio-test", version: "0" });
    try {
      await client.connect(transport);
      const res = (await client.callTool({ name: "list_vaults", arguments: {} })) as { content: { text: string }[] };
      expect(JSON.parse(res.content[0]!.text)[0]).toMatchObject({ name: "stdio-test", notes: 1 });
    } finally {
      await client.close();
      vault.cleanup();
    }
  });
});
