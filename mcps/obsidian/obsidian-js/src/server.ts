import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { registerDailyReadTools, registerDailyWriteTools } from "./tools/daily.js";
import { registerGraphTools } from "./tools/graph.js";
import { registerReadTools, registerWriteTools } from "./tools/notes.js";
import { registerSearchTools } from "./tools/search.js";
import type { VaultRegistry } from "./vault/vault.js";
import { VERSION } from "./version.js";

export function buildInstructions(registry: VaultRegistry): string {
  const vaults = registry
    .all()
    .map((v) => `- ${v.name}${v.name === registry.defaultName ? " (Standard)" : ""}${v.readOnly ? " [read-only]" : ""}`)
    .join("\n");
  return [
    "Zugriff auf Obsidian-Vaults (Markdown-Dateien mit YAML-Frontmatter, [[Wikilinks]], #tags).",
    `Vaults:\n${vaults}`,
    "Tipps:",
    "- Pfade sind vault-relativ (\"Ordner/Notiz.md\"). Beim Lesen reicht oft der Notizname – er wird wie ein [[Wikilink]] aufgelöst.",
    "- Erst suchen (search_notes, search_by_tag, search_by_frontmatter), dann gezielt lesen (read_note, ggf. mit section).",
    "- Für kleine Änderungen edit_note/append_to_note/update_frontmatter statt die ganze Notiz neu zu schreiben.",
    "- Beim Verlinken Obsidian-Syntax verwenden: [[Notiz]], [[Notiz#Überschrift]], [[Notiz|Alias]].",
    "- move_note aktualisiert alle Links automatisch; delete_note verschiebt standardmäßig in den Obsidian-Papierkorb.",
  ].join("\n");
}

export function createServer(registry: VaultRegistry): McpServer {
  const server = new McpServer(
    { name: "obsidian-vault-mcp", title: "Obsidian Vault MCP", version: VERSION },
    { instructions: buildInstructions(registry) },
  );
  registerReadTools(server, registry);
  registerSearchTools(server, registry);
  registerGraphTools(server, registry);
  registerDailyReadTools(server, registry);
  if (registry.anyWritable) {
    registerWriteTools(server, registry);
    registerDailyWriteTools(server, registry);
  }
  return server;
}
