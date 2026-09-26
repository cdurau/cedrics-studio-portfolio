import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { tagMatches } from "../vault/markdown.js";
import { VaultError, normalizeVaultPath } from "../vault/paths.js";
import type { VaultRegistry } from "../vault/vault.js";
import { getPath, inFolder, iso, json, safe, truncate, vaultParam } from "./util.js";

/** Zerlegt eine Suchanfrage in Begriffe; "in Anführungszeichen" bleibt zusammen. */
export function tokenizeQuery(q: string): string[] {
  const out: string[] = [];
  for (const m of q.matchAll(/"([^"]+)"|(\S+)/g)) {
    const t = (m[1] ?? m[2] ?? "").trim();
    if (t) out.push(t);
  }
  return out;
}

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

interface Match {
  line: number;
  text: string;
}

export function registerSearchTools(server: McpServer, registry: VaultRegistry): void {
  const vault = vaultParam(registry);

  server.registerTool(
    "search_notes",
    {
      title: "Volltextsuche",
      description:
        "Durchsucht Notizinhalte und Pfade. mode=all_words (Standard): alle Begriffe müssen vorkommen, \"Phrasen in Anführungszeichen\" bleiben zusammen. " +
        "mode=phrase: exakte Zeichenfolge. mode=regex: JavaScript-Regex. Ergebnisse sind nach Relevanz sortiert und enthalten Zeilennummern mit Kontext.",
      inputSchema: {
        vault,
        query: z.string().min(1),
        mode: z.enum(["all_words", "phrase", "regex"]).default("all_words"),
        caseSensitive: z.boolean().default(false),
        folder: z.string().optional().describe("Nur in diesem Ordner suchen"),
        tag: z.string().optional().describe("Nur Notizen mit diesem Tag (inkl. Untertags)"),
        searchIn: z.enum(["all", "content", "path"]).default("all"),
        contextLines: z.number().int().min(0).max(10).default(1),
        maxMatchesPerNote: z.number().int().min(1).max(50).default(5),
        limit: z.number().int().min(1).max(500).default(30),
      },
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    safe(async ({ vault: vName, query, mode, caseSensitive, folder, tag, searchIn, contextLines, maxMatchesPerNote, limit }) => {
      const v = registry.get(vName);
      const idx = await v.index();
      const f = folder ? normalizeVaultPath(folder) : "";
      const flags = caseSensitive ? "g" : "gi";

      let patterns: RegExp[];
      if (mode === "regex") {
        try {
          patterns = [new RegExp(query, `${flags}m`)];
        } catch (e) {
          throw new VaultError(`Ungültiger regulärer Ausdruck: ${(e as Error).message}`, "INVALID");
        }
      } else {
        const terms = mode === "phrase" ? [query] : tokenizeQuery(query);
        patterns = terms.map((t) => new RegExp(escapeRegex(t), flags));
      }
      const count = (re: RegExp, s: string, cap = 50): number => {
        re.lastIndex = 0;
        let n = 0;
        let m: RegExpExecArray | null;
        while (n < cap && (m = re.exec(s))) {
          n++;
          if (m[0] === "") re.lastIndex++;
        }
        re.lastIndex = 0;
        return n;
      };

      const results: { path: string; score: number; modified: string; matches: Match[]; mtime: number }[] = [];
      for (const note of idx.notes) {
        if (!inFolder(note.path, f)) continue;
        if (tag && !note.tags.some((t) => tagMatches(t, tag))) continue;
        let score = 0;
        let allFound = true;
        for (const re of patterns) {
          const inPath = searchIn !== "content" ? count(re, note.path) : 0;
          const inContent = searchIn !== "path" ? count(re, note.content) : 0;
          if (inPath + inContent === 0) {
            allFound = false;
            break;
          }
          score += Math.min(inContent, 20) + inPath * 10;
        }
        if (!allFound) continue;

        const matches: Match[] = [];
        if (searchIn !== "path") {
          const lines = note.content.split("\n");
          for (let i = 0; i < lines.length && matches.length < maxMatchesPerNote; i++) {
            const line = lines[i]!;
            if (patterns.some((re) => {
              re.lastIndex = 0;
              const hit = re.test(line);
              re.lastIndex = 0;
              return hit;
            })) {
              const from = Math.max(0, i - contextLines);
              const to = Math.min(lines.length, i + contextLines + 1);
              matches.push({ line: i + 1, text: lines.slice(from, to).map((l) => truncate(l, 300)).join("\n") });
            }
          }
        }
        results.push({ path: note.path, score, modified: iso(note.mtimeMs), matches, mtime: note.mtimeMs });
      }
      results.sort((a, b) => b.score - a.score || b.mtime - a.mtime);
      return json({
        query,
        mode,
        total: results.length,
        truncated: results.length > limit,
        results: results.slice(0, limit).map(({ mtime: _m, ...r }) => r),
      });
    }),
  );

  server.registerTool(
    "search_by_tag",
    {
      title: "Nach Tags suchen",
      description: "Findet Notizen mit bestimmten Tags (Frontmatter und #inline). Verschachtelte Tags (#projekt/aktiv) werden bei includeNested mitgefunden.",
      inputSchema: {
        vault,
        tags: z.array(z.string()).min(1).describe("Tags mit oder ohne #"),
        match: z.enum(["any", "all"]).default("any"),
        includeNested: z.boolean().default(true),
        folder: z.string().optional(),
        limit: z.number().int().min(1).max(2000).default(200),
      },
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    safe(async ({ vault: vName, tags, match, includeNested, folder, limit }) => {
      const v = registry.get(vName);
      const idx = await v.index();
      const f = folder ? normalizeVaultPath(folder) : "";
      const hits = idx.notes.filter((n) => {
        if (!inFolder(n.path, f)) return false;
        const has = (q: string) => n.tags.some((t) => tagMatches(t, q, includeNested));
        return match === "all" ? tags.every(has) : tags.some(has);
      });
      hits.sort((a, b) => b.mtimeMs - a.mtimeMs);
      return json({
        total: hits.length,
        truncated: hits.length > limit,
        results: hits.slice(0, limit).map((n) => ({ path: n.path, tags: n.tags, modified: iso(n.mtimeMs) })),
      });
    }),
  );

  server.registerTool(
    "search_by_frontmatter",
    {
      title: "Nach Properties suchen",
      description:
        "Findet Notizen anhand von Frontmatter-Properties. `key` unterstützt Punktnotation (\"projekt.status\"). " +
        "Operatoren: exists, missing, equals, not_equals, contains (Text/Listen), gt/gte/lt/lte (Zahlen und ISO-Datumsangaben).",
      inputSchema: {
        vault,
        key: z.string().min(1),
        operator: z.enum(["exists", "missing", "equals", "not_equals", "contains", "gt", "gte", "lt", "lte"]).default("exists"),
        value: z.union([z.string(), z.number(), z.boolean()]).optional(),
        folder: z.string().optional(),
        limit: z.number().int().min(1).max(2000).default(200),
      },
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    safe(async ({ vault: vName, key, operator, value, folder, limit }) => {
      if (!["exists", "missing"].includes(operator) && value === undefined) {
        throw new VaultError(`Operator "${operator}" braucht einen value`, "INVALID");
      }
      const v = registry.get(vName);
      const idx = await v.index();
      const f = folder ? normalizeVaultPath(folder) : "";
      const hits: { path: string; value: unknown }[] = [];
      for (const n of idx.notes) {
        if (!inFolder(n.path, f)) continue;
        const actual = getPath(n.frontmatter, key);
        if (matchesProperty(actual, operator, value)) hits.push({ path: n.path, value: actual });
      }
      return json({ total: hits.length, truncated: hits.length > limit, results: hits.slice(0, limit) });
    }),
  );

  server.registerTool(
    "list_tags",
    {
      title: "Tags auflisten",
      description: "Listet alle Tags im Vault (oder Ordner) mit Häufigkeit.",
      inputSchema: {
        vault,
        folder: z.string().optional(),
        sortBy: z.enum(["count", "name"]).default("count"),
        minCount: z.number().int().min(1).default(1),
      },
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    safe(async ({ vault: vName, folder, sortBy, minCount }) => {
      const v = registry.get(vName);
      const idx = await v.index();
      const f = folder ? normalizeVaultPath(folder) : "";
      const counts = new Map<string, { tag: string; count: number }>();
      for (const n of idx.notes) {
        if (!inFolder(n.path, f)) continue;
        for (const t of n.tags) {
          const k = t.toLowerCase();
          const e = counts.get(k);
          if (e) e.count++;
          else counts.set(k, { tag: t, count: 1 });
        }
      }
      const list = [...counts.values()].filter((e) => e.count >= minCount);
      list.sort((a, b) => (sortBy === "count" ? b.count - a.count || a.tag.localeCompare(b.tag) : a.tag.localeCompare(b.tag)));
      return json({ total: list.length, tags: list });
    }),
  );
}

function asComparable(v: unknown): number | string | undefined {
  if (typeof v === "number") return v;
  if (v instanceof Date) return v.toISOString();
  if (typeof v === "string") {
    const n = Number(v);
    if (v.trim() !== "" && !Number.isNaN(n)) return n;
    return v;
  }
  if (typeof v === "boolean") return v ? 1 : 0;
  return undefined;
}

function eq(a: unknown, b: unknown): boolean {
  if (typeof a === "string" && typeof b === "string") return a.toLowerCase() === b.toLowerCase();
  if (a instanceof Date) return a.toISOString().startsWith(String(b));
  return String(a).toLowerCase() === String(b).toLowerCase();
}

export function matchesProperty(actual: unknown, operator: string, value: unknown): boolean {
  const present = actual !== undefined && actual !== null && actual !== "";
  switch (operator) {
    case "exists":
      return present;
    case "missing":
      return !present;
    case "equals":
      return Array.isArray(actual) ? actual.some((a) => eq(a, value)) : present && eq(actual, value);
    case "not_equals":
      return Array.isArray(actual) ? !actual.some((a) => eq(a, value)) : !present || !eq(actual, value);
    case "contains": {
      const needle = String(value).toLowerCase();
      if (Array.isArray(actual)) return actual.some((a) => String(a).toLowerCase().includes(needle));
      return present && String(actual).toLowerCase().includes(needle);
    }
    case "gt":
    case "gte":
    case "lt":
    case "lte": {
      const a = asComparable(actual);
      const b = asComparable(value);
      if (a === undefined || b === undefined || typeof a !== typeof b) return false;
      if (operator === "gt") return a > b;
      if (operator === "gte") return a >= b;
      if (operator === "lt") return a < b;
      return a <= b;
    }
    default:
      return false;
  }
}

