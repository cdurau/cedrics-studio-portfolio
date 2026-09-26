import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import path from "node:path";
import { z } from "zod";
import { applyEdits, insertContent, replaceSection, updateFrontmatter, withFrontmatter } from "../vault/edit.js";
import { findSection } from "../vault/markdown.js";
import { VaultError, isNotePath, noteBasename, normalizeVaultPath } from "../vault/paths.js";
import { LinkResolver, Vault, type VaultRegistry } from "../vault/vault.js";
import { iso, json, safe, text, truncate, vaultParam } from "./util.js";

export function registerReadTools(server: McpServer, registry: VaultRegistry): void {
  const vault = vaultParam(registry);

  server.registerTool(
    "list_vaults",
    {
      title: "Vaults auflisten",
      description: "Listet alle konfigurierten Obsidian-Vaults mit Name, Schreibschutz, Anzahl Notizen und Daily-Notes-Einstellungen.",
      inputSchema: {},
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    safe(async () => {
      const out = await Promise.all(
        registry.all().map(async (v) => {
          const idx = await v.index();
          return {
            name: v.name,
            default: v.name === registry.defaultName,
            readOnly: v.readOnly,
            notes: idx.notes.length,
            files: idx.files.length,
            dailyNotes: await v.dailyNotesSettings(),
          };
        }),
      );
      return json(out);
    }),
  );

  server.registerTool(
    "list_notes",
    {
      title: "Notizen auflisten",
      description:
        "Listet Notizen (und optional Anhänge/Ordner) eines Vaults oder Ordners. Pfade sind vault-relativ, z. B. \"Projekte/Idee.md\".",
      inputSchema: {
        vault,
        folder: z.string().optional().describe("Ordner relativ zum Vault. Leer = gesamter Vault"),
        recursive: z.boolean().default(true).describe("Unterordner einbeziehen"),
        includeAttachments: z.boolean().default(false).describe("Auch Nicht-Markdown-Dateien (Bilder, PDFs, Canvas …) listen"),
        includeFolders: z.boolean().default(false).describe("Zusätzlich die Ordnerliste zurückgeben"),
        sortBy: z.enum(["path", "modified"]).default("path"),
        limit: z.number().int().min(1).max(5000).default(500),
      },
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    safe(async ({ vault: vName, folder, recursive, includeAttachments, includeFolders, sortBy, limit }) => {
      const v = registry.get(vName);
      const f = folder ? normalizeVaultPath(folder) : "";
      if (f && (await v.isFile(f))) throw new VaultError(`"${f}" ist eine Datei, kein Ordner`, "INVALID");
      let files = await v.walk(f);
      if (!recursive) files = files.filter((x) => path.posix.dirname(x.path) === (f || "."));
      if (!includeAttachments) files = files.filter((x) => x.isNote);
      if (sortBy === "modified") files.sort((a, b) => b.mtimeMs - a.mtimeMs);
      const items = files.slice(0, limit).map((x) => ({ path: x.path, modified: iso(x.mtimeMs), size: x.size }));
      const result: Record<string, unknown> = { vault: v.name, folder: f || "/", total: files.length, truncated: files.length > limit, items };
      if (includeFolders) {
        let folders = await v.listFolders(f);
        if (!recursive) folders = folders.filter((d) => path.posix.dirname(d) === (f || "."));
        result.folders = folders;
      }
      return json(result);
    }),
  );

  server.registerTool(
    "read_note",
    {
      title: "Notiz lesen",
      description:
        "Liest eine Notiz. `path` darf ein Pfad (\"Ordner/Notiz.md\"), ein Pfad ohne .md oder nur der Notizname sein (Auflösung wie bei [[Wikilinks]]). " +
        "Mit `section` wird nur der Abschnitt unter einer Überschrift geliefert (verschachtelt: \"Projekte#Aktiv\"). " +
        "Mit includeMetadata kommen Frontmatter, Tags, Überschriften und Links dazu.",
      inputSchema: {
        vault,
        path: z.string().describe("Pfad oder Name der Notiz"),
        section: z.string().optional().describe("Nur diesen Abschnitt (Überschrift) lesen"),
        includeMetadata: z.boolean().default(false),
      },
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    safe(async ({ vault: vName, path: ref, section, includeMetadata }) => {
      const v = registry.get(vName);
      const rel = await v.resolveExisting(ref);
      const content = await v.readFile(rel);
      let body = content;
      if (section) {
        const range = findSection(content, section);
        if (!range) throw new VaultError(`Abschnitt "${section}" nicht gefunden in ${rel}`, "NOT_FOUND");
        body = content.slice(range.contentStart, range.end);
      }
      if (!includeMetadata) {
        const resolvedNote = rel !== ref.trim() ? [{ type: "text" as const, text: `(aufgelöster Pfad: ${rel})` }] : [];
        return { content: [{ type: "text", text: body }, ...resolvedNote] };
      }
      const idx = await v.index();
      const info = idx.notesByPath.get(rel);
      const meta = info
        ? {
            path: rel,
            modified: iso(info.mtimeMs),
            created: iso(info.ctimeMs),
            size: info.size,
            frontmatter: info.frontmatter,
            frontmatterError: info.frontmatterError,
            tags: info.tags,
            headings: info.headings.map((h) => `${"#".repeat(h.level)} ${h.text}`),
            outgoingLinks: [...new Set(info.links.map((l) => idx.resolver.resolve(l.target, rel, l.kind) ?? `${l.target} (unaufgelöst)`))],
          }
        : { path: rel };
      return { content: [{ type: "text", text: JSON.stringify(meta, null, 2) }, { type: "text", text: body }] };
    }),
  );

  server.registerTool(
    "read_multiple_notes",
    {
      title: "Mehrere Notizen lesen",
      description: "Liest bis zu 25 Notizen auf einmal. Fehlende Notizen werden gemeldet, brechen aber nicht ab.",
      inputSchema: {
        vault,
        paths: z.array(z.string()).min(1).max(25),
        maxCharsPerNote: z.number().int().min(100).max(200000).default(20000),
      },
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    safe(async ({ vault: vName, paths, maxCharsPerNote }) => {
      const v = registry.get(vName);
      const parts = await Promise.all(
        paths.map(async (ref) => {
          try {
            const rel = await v.resolveExisting(ref);
            const c = await v.readFile(rel);
            return `===== ${rel} =====\n${truncate(c, maxCharsPerNote)}`;
          } catch (e) {
            return `===== ${ref} =====\n[FEHLER] ${(e as Error).message}`;
          }
        }),
      );
      return text(parts.join("\n\n"));
    }),
  );

  server.registerTool(
    "recent_notes",
    {
      title: "Zuletzt geänderte Notizen",
      description: "Listet die zuletzt geänderten Notizen, optional nur in einem Ordner oder seit einer bestimmten Anzahl Tage.",
      inputSchema: {
        vault,
        limit: z.number().int().min(1).max(500).default(20),
        folder: z.string().optional(),
        sinceDays: z.number().min(0).optional().describe("Nur Notizen, die in den letzten N Tagen geändert wurden"),
      },
      annotations: { readOnlyHint: true, openWorldHint: false },
    },
    safe(async ({ vault: vName, limit, folder, sinceDays }) => {
      const v = registry.get(vName);
      const f = folder ? normalizeVaultPath(folder) : "";
      const cutoff = sinceDays !== undefined ? Date.now() - sinceDays * 86400000 : 0;
      const files = (await v.walk(f)).filter((x) => x.isNote && x.mtimeMs >= cutoff).sort((a, b) => b.mtimeMs - a.mtimeMs);
      return json(files.slice(0, limit).map((x) => ({ path: x.path, modified: iso(x.mtimeMs) })));
    }),
  );
}

export function registerWriteTools(server: McpServer, registry: VaultRegistry): void {
  const vault = vaultParam(registry);

  server.registerTool(
    "write_note",
    {
      title: "Notiz schreiben",
      description:
        "Legt eine neue Notiz an (fehlende Ordner werden erstellt). Überschreibt eine bestehende Notiz nur mit overwrite=true. " +
        "`.md` wird automatisch ergänzt. Optionales `frontmatter` wird mit einem eventuell im Inhalt vorhandenen Frontmatter zusammengeführt.",
      inputSchema: {
        vault,
        path: z.string().describe("Vault-relativer Pfad, z. B. \"Projekte/Neue Idee\""),
        content: z.string().describe("Markdown-Inhalt"),
        frontmatter: z.record(z.string(), z.any()).optional().describe("YAML-Properties als Objekt, z. B. {\"tags\": [\"projekt\"], \"status\": \"aktiv\"}"),
        overwrite: z.boolean().default(false),
      },
      annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: true, openWorldHint: false },
    },
    safe(async ({ vault: vName, path: ref, content, frontmatter, overwrite }) => {
      const v = registry.get(vName);
      const rel = v.newNotePath(ref);
      const full = withFrontmatter(content, frontmatter);
      const { created } = await v.writeFile(rel, full.endsWith("\n") || full === "" ? full : `${full}\n`, { overwrite });
      return json({ path: rel, created, overwritten: !created });
    }),
  );

  server.registerTool(
    "append_to_note",
    {
      title: "An Notiz anhängen",
      description:
        "Fügt Text am Ende (oder Anfang) einer Notiz ein – optional innerhalb eines Abschnitts unter einer Überschrift. " +
        "position=start ohne Überschrift fügt direkt nach dem Frontmatter ein.",
      inputSchema: {
        vault,
        path: z.string(),
        content: z.string(),
        heading: z.string().optional().describe("Überschrift, unter der eingefügt wird (verschachtelt: \"Log#2026\")"),
        position: z.enum(["end", "start"]).default("end"),
        createIfMissing: z.boolean().default(false).describe("Notiz anlegen, falls sie fehlt"),
        createHeading: z.boolean().default(false).describe("Überschrift am Ende anlegen, falls sie fehlt"),
      },
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false },
    },
    safe(async ({ vault: vName, path: ref, content, heading, position, createIfMissing, createHeading }) => {
      const v = registry.get(vName);
      v.assertWritable();
      let rel: string;
      let existing = "";
      let created = false;
      try {
        rel = await v.resolveExisting(ref);
        existing = await v.readFile(rel);
      } catch (e) {
        if (!(e instanceof VaultError && e.code === "NOT_FOUND") || !createIfMissing) throw e;
        rel = v.newNotePath(ref);
        created = true;
      }
      const updated = insertContent(existing, content, { heading, position, createHeading: createHeading || created });
      await v.writeFile(rel, updated, { overwrite: true });
      return json({ path: rel, created });
    }),
  );

  server.registerTool(
    "edit_note",
    {
      title: "Notiz bearbeiten (Suchen & Ersetzen)",
      description:
        "Präzise Änderungen per exaktem Textersatz. Jeder oldText muss genau einmal vorkommen (sonst replaceAll=true). " +
        "Alle Edits werden atomar angewendet – schlägt einer fehl, wird nichts geschrieben. dryRun=true zeigt nur das Ergebnis.",
      inputSchema: {
        vault,
        path: z.string(),
        edits: z
          .array(
            z.object({
              oldText: z.string().describe("Exakter Text inkl. Leerzeichen/Zeilenumbrüchen"),
              newText: z.string(),
              replaceAll: z.boolean().optional(),
            }),
          )
          .min(1),
        dryRun: z.boolean().default(false),
      },
      annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: false, openWorldHint: false },
    },
    safe(async ({ vault: vName, path: ref, edits, dryRun }) => {
      const v = registry.get(vName);
      if (!dryRun) v.assertWritable();
      const rel = await v.resolveExisting(ref);
      const before = await v.readFile(rel);
      const { content, replacements } = applyEdits(before, edits);
      if (dryRun) return text(`[dryRun] ${replacements} Ersetzung(en) in ${rel}. Ergebnis:\n\n${content}`);
      await v.writeFile(rel, content, { overwrite: true });
      return json({ path: rel, replacements });
    }),
  );

  server.registerTool(
    "replace_section",
    {
      title: "Abschnitt ersetzen",
      description: "Ersetzt den kompletten Inhalt unter einer Überschrift (bis zur nächsten gleich- oder höherrangigen Überschrift). Die Überschrift selbst bleibt erhalten.",
      inputSchema: {
        vault,
        path: z.string(),
        heading: z.string().describe("Überschrift, z. B. \"Aufgaben\" oder \"Projekte#Aktiv\""),
        content: z.string().describe("Neuer Inhalt des Abschnitts (ohne die Überschrift)"),
      },
      annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: true, openWorldHint: false },
    },
    safe(async ({ vault: vName, path: ref, heading, content }) => {
      const v = registry.get(vName);
      v.assertWritable();
      const rel = await v.resolveExisting(ref);
      const updated = replaceSection(await v.readFile(rel), heading, content);
      await v.writeFile(rel, updated, { overwrite: true });
      return json({ path: rel, heading });
    }),
  );

  server.registerTool(
    "update_frontmatter",
    {
      title: "Properties/Frontmatter ändern",
      description:
        "Ändert YAML-Properties einer Notiz, ohne den Inhalt anzufassen. `set` setzt Werte (null entfernt den Key), `remove` löscht Keys, " +
        "`addTags`/`removeTags` pflegen die tags-Liste.",
      inputSchema: {
        vault,
        path: z.string(),
        set: z.record(z.string(), z.any()).optional(),
        remove: z.array(z.string()).optional(),
        addTags: z.array(z.string()).optional(),
        removeTags: z.array(z.string()).optional(),
      },
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false },
    },
    safe(async ({ vault: vName, path: ref, set, remove, addTags, removeTags }) => {
      const v = registry.get(vName);
      v.assertWritable();
      const rel = await v.resolveExisting(ref);
      if (!isNotePath(rel)) throw new VaultError("Frontmatter gibt es nur in Markdown-Notizen", "INVALID");
      const { content, frontmatter } = updateFrontmatter(await v.readFile(rel), { set, remove, addTags, removeTags });
      await v.writeFile(rel, content, { overwrite: true });
      return json({ path: rel, frontmatter });
    }),
  );

  server.registerTool(
    "move_note",
    {
      title: "Notiz verschieben/umbenennen",
      description:
        "Verschiebt oder benennt eine Notiz (oder einen Anhang) um und aktualisiert – wie Obsidian – alle Wikilinks, Embeds und Markdown-Links im Vault. " +
        "Endet `to` auf \"/\" oder ist ein bestehender Ordner, wird die Datei dorthin verschoben und behält ihren Namen.",
      inputSchema: {
        vault,
        from: z.string(),
        to: z.string().describe("Neuer Pfad oder Zielordner"),
        updateLinks: z.boolean().default(true),
        overwrite: z.boolean().default(false),
      },
      annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: false, openWorldHint: false },
    },
    safe(async ({ vault: vName, from, to, updateLinks, overwrite }) => {
      const v = registry.get(vName);
      v.assertWritable();
      const src = await v.resolveExisting(from);
      const toNorm = normalizeVaultPath(to);
      let dest: string;
      const toIsDir = to.trim().endsWith("/") || toNorm === "" || ((await v.exists(toNorm)) && !(await v.isFile(toNorm)));
      if (toIsDir) {
        dest = toNorm ? `${toNorm}/${path.posix.basename(src)}` : path.posix.basename(src);
      } else {
        const srcExt = path.posix.extname(src);
        dest = path.posix.extname(toNorm).toLowerCase() === srcExt.toLowerCase() ? toNorm : `${toNorm}${srcExt}`;
      }
      if (dest === src) return json({ from: src, to: dest, moved: false, updatedNotes: [] });

      const before = await v.index();
      const afterPaths = before.files.map((f) => (f.path === src ? dest : f.path)).filter((p, i, arr) => arr.indexOf(p) === i);
      const after = new LinkResolver(afterPaths);

      await v.moveFile(src, dest, { overwrite });

      const updatedNotes: { path: string; links: number }[] = [];
      const failed: { path: string; error: string }[] = [];
      if (updateLinks) {
        for (const note of before.notes) {
          const isMoved = note.path === src;
          const references = note.links.some((l) => l.target && before.resolver.resolve(l.target, note.path, l.kind) === src);
          if (!references && !isMoved) continue;
          const currentPath = isMoved ? dest : note.path;
          try {
            const content = await v.readFile(currentPath);
            const res = Vault.rewriteLinks({
              content,
              sourceOld: note.path,
              sourceNew: currentPath,
              oldTarget: src,
              newTarget: dest,
              before: before.resolver,
              after,
            });
            if (res) {
              await v.writeFile(currentPath, res.content, { overwrite: true });
              updatedNotes.push({ path: currentPath, links: res.changed });
            }
          } catch (e) {
            failed.push({ path: currentPath, error: (e as Error).message });
          }
        }
      }
      return json({ from: src, to: dest, moved: true, updatedNotes, ...(failed.length ? { failed } : {}) });
    }),
  );

  server.registerTool(
    "delete_note",
    {
      title: "Notiz löschen",
      description:
        "Löscht eine Notiz oder Datei. Standard: Verschieben in den Obsidian-Papierkorb (<vault>/.trash), wiederherstellbar. " +
        "permanent=true löscht endgültig. Meldet, wie viele Notizen noch auf die gelöschte Datei verlinken.",
      inputSchema: {
        vault,
        path: z.string(),
        permanent: z.boolean().default(false),
      },
      annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: false, openWorldHint: false },
    },
    safe(async ({ vault: vName, path: ref, permanent }) => {
      const v = registry.get(vName);
      v.assertWritable();
      const rel = await v.resolveExisting(ref);
      const idx = await v.index();
      const linkedFrom = idx.notes
        .filter((n) => n.path !== rel && n.links.some((l) => l.target && idx.resolver.resolve(l.target, n.path, l.kind) === rel))
        .map((n) => n.path);
      const trashPath = await v.deleteFile(rel, permanent);
      return json({
        deleted: rel,
        permanent,
        trashPath,
        brokenLinksFrom: linkedFrom,
        hint: linkedFrom.length ? `${linkedFrom.length} Notiz(en) verlinken noch auf "${noteBasename(rel)}".` : undefined,
      });
    }),
  );
}
