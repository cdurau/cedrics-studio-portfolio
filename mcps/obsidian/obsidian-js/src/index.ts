#!/usr/bin/env node
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { ConfigError, HELP_TEXT, loadConfig, parseArgs } from "./config.js";
import { startHttpServer } from "./http.js";
import { createServer } from "./server.js";
import { VaultRegistry } from "./vault/vault.js";
import { VERSION } from "./version.js";

// Wichtig: Bei stdio gehört stdout exklusiv dem MCP-Protokoll → Logs nur auf stderr.
const log = (msg: string) => process.stderr.write(`${msg}\n`);

async function main(): Promise<void> {
  const argv = process.argv.slice(2);
  const quick = (() => {
    try {
      return parseArgs(argv);
    } catch {
      return null;
    }
  })();
  if (quick?.help) {
    process.stdout.write(HELP_TEXT);
    return;
  }
  if (quick?.version) {
    process.stdout.write(`${VERSION}\n`);
    return;
  }

  const config = loadConfig(argv);
  const registry = await VaultRegistry.create(config.vaults, {
    defaultVault: config.defaultVault,
    locale: config.locale,
    trash: config.trash,
  });
  const summary = config.vaults.map((v) => `${v.name}${v.readOnly ? " (ro)" : ""} → ${v.path}`).join(", ");

  if (config.transport === "http") {
    const started = await startHttpServer(registry, config.http, log);
    log(`[obsidian-vault-mcp] v${VERSION} Vaults: ${summary}`);
    const shutdown = async (signal: string) => {
      log(`[obsidian-vault-mcp] ${signal} – beende …`);
      await started.close();
      process.exit(0);
    };
    process.on("SIGINT", () => void shutdown("SIGINT"));
    process.on("SIGTERM", () => void shutdown("SIGTERM"));
    return;
  }

  const server = createServer(registry);
  await server.connect(new StdioServerTransport());
  log(`[obsidian-vault-mcp] v${VERSION} stdio bereit. Vaults: ${summary}`);
}

main().catch((e) => {
  if (e instanceof ConfigError) {
    log(`Konfigurationsfehler: ${e.message}\n\n${HELP_TEXT}`);
    process.exit(2);
  }
  log(`[obsidian-vault-mcp] Fataler Fehler: ${(e as Error).stack ?? e}`);
  process.exit(1);
});
