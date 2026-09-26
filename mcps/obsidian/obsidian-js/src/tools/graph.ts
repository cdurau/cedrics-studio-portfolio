import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { normalizeVaultPath } from "../vault/paths.js";
import type { VaultRegistry } from "../vault/vault.js";
import { inFolder, json, safe, truncate, vaultParam } from "./util.js";

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function registerGraphTools(server: McpServer, registry: VaultRegistry): void {
  const vault = vaultParam(registry);

  server.registerTool(
    "get_backlinks",
    {
      title: "Backlinks",
      description:
        "Zeigt alle Notizen, die auf eine Notiz verlinken (Wikilinks, Embeds, Markdown-Links, Frontmatter-Links) – mit Zeilenkontext. " +
        "Optional auch unverlinkte Erwähnungen (Notizname oder Alias im Text, aber ohne Link).",
      inputSchema: {
        vault,
        path: z.string().describe("Pfad oder Name der Zielnotiz"),
        includeContext: z.boolean().default(true),
        includeUnlinkedMentions: z.boolean().default(false),
        limit: z.number().int().min(1).max(1000).default(100),
      },
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    safe(async ({ vault: vName, path: ref, includeContext, includeUnlinkedMentions, limit }) => {
      const v = registry.get(vName);
      const target = await v.resolveExisting(ref);
      const idx = await v.index();
      const linked: { source: string; count: number; mentions?: { line: number; text: string }[] }[] = [];
      const linkingSources = new Set<string>();

      for (const n of idx.notes) {
        if (n.path === target) continue;
        const hits = n.links.filter((l) => l.target && idx.resolver.resolve(l.target, n.path, l.kind) === target);
        if (hits.length === 0) continue;
        linkingSources.add(n.path);
        const entry: (typeof linked)[number] = { source: n.path, count: hits.length };
        if (includeContext) {
          const lines = n.content.split("\n");
          const seenLines = new Set<number>();
          entry.mentions = hits
            .filter((h) => (seenLines.has(h.line) ? false : (seenLines.add(h.line), true)))
            .slice(0, 10)
            .map((h) => ({ line: h.line, text: truncate(lines[h.line - 1]?.trim() ?? "", 300) }));
        }
        linked.push(entry);
      }
      linked.sort((a, b) => b.count - a.count || a.source.localeCompare(b.source));

      const result: Record<string, unknown> = {
        target,
        total: linked.length,
        truncated: linked.length > limit,
        backlinks: linked.slice(0, limit),
      };

      if (includeUnlinkedMentions) {
        const info = idx.notesByPath.get(target);
        const names = new Set<string>([info?.name ?? target.replace(/\.[^.]+$/, "").split("/").pop()!]);
        const aliases = info?.frontmatter?.aliases ?? info?.frontmatter?.alias;
        for (const a of Array.isArray(aliases) ? aliases : aliases ? [aliases] : []) if (String(a).trim()) names.add(String(a).trim());
        const re = new RegExp(`(?<![\\p{L}\\p{N}])(?:${[...names].map(escapeRegex).join("|")})(?![\\p{L}\\p{N}])`, "iu");
        const unlinked: { source: string; line: number; text: string }[] = [];
        for (const n of idx.notes) {
          if (n.path === target || linkingSources.has(n.path)) continue;
          const lines = n.content.split("\n");
          for (let i = 0; i < lines.length; i++) {
            const line = lines[i]!;
            // Zeilen mit Links auf andere Ziele trotzdem prüfen, aber Wikilink-Inhalte ausblenden
            const stripped = line.replace(/!?\[\[[^\]]*\]\]/g, " ");
            if (re.test(stripped)) {
              unlinked.push({ source: n.path, line: i + 1, text: truncate(line.trim(), 300) });
              break;
            }
          }
          if (unlinked.length >= limit) break;
        }
        result.unlinkedMentions = unlinked;
      }
      return json(result);
    }),
  );

  server.registerTool(
    "get_outgoing_links",
    {
      title: "Ausgehende Links",
      description: "Listet alle Links einer Notiz mit aufgelöstem Ziel (oder null, wenn das Ziel nicht existiert).",
      inputSchema: {
        vault,
        path: z.string(),
        onlyUnresolved: z.boolean().default(false),
      },
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    safe(async ({ vault: vName, path: ref, onlyUnresolved }) => {
      const v = registry.get(vName);
      const rel = await v.resolveExisting(ref);
      const idx = await v.index();
      const note = idx.notesByPath.get(rel);
      if (!note) return json({ path: rel, links: [] });
      const links = note.links
        .map((l) => ({
          target: l.target || "(selbst)",
          resolved: l.target ? idx.resolver.resolve(l.target, rel, l.kind) : rel,
          subpath: l.subpath,
          alias: l.alias,
          embed: l.embed,
          kind: l.kind,
          line: l.line,
          inFrontmatter: l.inFrontmatter || undefined,
        }))
        .filter((l) => !onlyUnresolved || l.resolved === null);
      return json({ path: rel, total: links.length, links });
    }),
  );

  server.registerTool(
    "find_orphans",
    {
      title: "Verwaiste Notizen",
      description:
        "Findet Notizen ohne eingehende Links (optional auch ohne ausgehende = komplett isoliert) und optional ungenutzte Anhänge.",
      inputSchema: {
        vault,
        folder: z.string().optional(),
        onlyIsolated: z.boolean().default(false).describe("Nur Notizen ohne eingehende UND ausgehende Links"),
        includeAttachments: z.boolean().default(false).describe("Auch nirgends eingebettete/verlinkte Anhänge melden"),
        limit: z.number().int().min(1).max(5000).default(200),
      },
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    safe(async ({ vault: vName, folder, onlyIsolated, includeAttachments, limit }) => {
      const v = registry.get(vName);
      const idx = await v.index();
      const f = folder ? normalizeVaultPath(folder) : "";
      const incoming = new Set<string>();
      const hasOutgoing = new Set<string>();
      for (const n of idx.notes) {
        for (const l of n.links) {
          if (!l.target) continue;
          const r = idx.resolver.resolve(l.target, n.path, l.kind);
          if (r && r !== n.path) {
            incoming.add(r);
            hasOutgoing.add(n.path);
          }
        }
      }
      const orphans = idx.notes
        .filter((n) => inFolder(n.path, f) && !incoming.has(n.path) && (!onlyIsolated || !hasOutgoing.has(n.path)))
        .map((n) => ({ path: n.path, hasOutgoingLinks: hasOutgoing.has(n.path) }));
      const result: Record<string, unknown> = { total: orphans.length, truncated: orphans.length > limit, orphans: orphans.slice(0, limit) };
      if (includeAttachments) {
        result.unusedAttachments = idx.files
          .filter((x) => !x.isNote && inFolder(x.path, f) && !incoming.has(x.path))
          .slice(0, limit)
          .map((x) => x.path);
      }
      return json(result);
    }),
  );

  server.registerTool(
    "find_unresolved_links",
    {
      title: "Unaufgelöste Links",
      description: "Findet Links auf nicht existierende Notizen/Dateien, gruppiert nach Ziel – ideal, um fehlende Notizen anzulegen oder Tippfehler zu finden.",
      inputSchema: {
        vault,
        folder: z.string().optional().describe("Nur Links aus Notizen in diesem Ordner"),
        limit: z.number().int().min(1).max(5000).default(200),
      },
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    safe(async ({ vault: vName, folder, limit }) => {
      const v = registry.get(vName);
      const idx = await v.index();
      const f = folder ? normalizeVaultPath(folder) : "";
      const groups = new Map<string, { target: string; count: number; sources: Set<string> }>();
      for (const n of idx.notes) {
        if (!inFolder(n.path, f)) continue;
        for (const l of n.links) {
          if (!l.target || idx.resolver.resolve(l.target, n.path, l.kind)) continue;
          const key = l.target.toLowerCase();
          const g = groups.get(key) ?? { target: l.target, count: 0, sources: new Set<string>() };
          g.count++;
          g.sources.add(n.path);
          groups.set(key, g);
        }
      }
      const list = [...groups.values()]
        .sort((a, b) => b.count - a.count || a.target.localeCompare(b.target))
        .map((g) => ({ target: g.target, count: g.count, sources: [...g.sources].slice(0, 20) }));
      return json({ total: list.length, truncated: list.length > limit, unresolved: list.slice(0, limit) });
    }),
  );
}
