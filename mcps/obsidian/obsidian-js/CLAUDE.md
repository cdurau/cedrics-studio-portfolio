# obsidian-vault-mcp – Projektkontext

MCP-Server (TypeScript, `@modelcontextprotocol/sdk` 1.x, zod 4) für Obsidian Vaults. Arbeitet direkt auf dem Dateisystem, kein Obsidian-Plugin. Siehe README für Nutzung.

## Befehle
- `npm run check` – Typecheck + alle Tests + Bundle (vor jedem Commit)
- `npm test` / `npm run test:watch` – Vitest
- `npm run build` – Typecheck + `dist/obsidian-vault-mcp.mjs` (esbuild, ohne node_modules lauffähig)
- `npm run dev -- --vault name=/pfad [--http]`

## Architektur
- `src/vault/` ist frei von MCP – reine Vault-Logik, direkt unit-testbar.
- `src/tools/*` registrieren Tools; jeder Handler ist in `safe()` gewickelt (Fehler → `isError`-Result).
- Schreib-Tools werden nur registriert, wenn mindestens ein Vault schreibbar ist; zusätzlich prüft jeder Schreibpfad `vault.assertWritable()`.
- HTTP ist stateless: pro Request neue `McpServer`-Instanz + Transport.
- Index: `Vault.index()` scannt bei jedem Aufruf die mtimes, liest nur geänderte Dateien neu. Nach Schreibzugriffen `invalidate()`.

## Regeln
- Alle Pfade von außen über `normalizeVaultPath()` + `vault.abs()` (Traversal-/Symlink-Schutz). Versteckte Pfade bleiben gesperrt.
- stdout gehört bei stdio dem Protokoll → Logs nur auf stderr.
- Links immer über `LinkResolver` auflösen, nie selbst Pfade raten.
- Neue Tools: Test in `test/e2e.test.ts` und Eintrag in der README-Tabelle.
- Tool-Beschreibungen und Fehlermeldungen auf Deutsch.
