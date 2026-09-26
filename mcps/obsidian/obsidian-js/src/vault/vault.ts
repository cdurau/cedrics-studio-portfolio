import { randomBytes } from "node:crypto";
import { promises as fs } from "node:fs";
import path from "node:path";
import type { DailyNotesOverride, VaultConfig } from "../config.js";
import {
  extractHeadings,
  extractLinks,
  extractTags,
  parseFrontmatter,
  type Frontmatter,
  type Heading,
  type LinkRef,
} from "./markdown.js";
import {
  IGNORED_DIRS,
  TEXT_EXTENSIONS,
  VaultError,
  ensureNoteExtension,
  isNotePath,
  noteBasename,
  normalizeVaultPath,
  stripNoteExt,
  toAbsoluteSafe,
} from "./paths.js";

export interface FileEntry {
  path: string;
  mtimeMs: number;
  ctimeMs: number;
  size: number;
  isNote: boolean;
}

export interface NoteInfo {
  path: string;
  name: string;
  mtimeMs: number;
  ctimeMs: number;
  size: number;
  content: string;
  frontmatter: Frontmatter | null;
  frontmatterError?: string;
  tags: string[];
  links: LinkRef[];
  headings: Heading[];
}

export interface VaultIndex {
  files: FileEntry[];
  notes: NoteInfo[];
  notesByPath: Map<string, NoteInfo>;
  resolver: LinkResolver;
}

export interface DailyNotesSettings {
  folder: string;
  format: string;
  template?: string;
  source: "config" | "periodic-notes" | "daily-notes" | "default";
}

/** Löst Obsidian-Linkziele ([[Notiz]], [[Ordner/Notiz]], [text](../x.md)) zu Vault-Pfaden auf. */
export class LinkResolver {
  private byPathLower = new Map<string, string>();
  private byNameLower = new Map<string, string[]>();

  constructor(readonly paths: string[]) {
    for (const p of paths) {
      this.byPathLower.set(p.toLowerCase(), p);
      const base = path.posix.basename(p).toLowerCase();
      const list = this.byNameLower.get(base);
      if (list) list.push(p);
      else this.byNameLower.set(base, [p]);
    }
  }

  has(p: string): boolean {
    return this.byPathLower.has(p.toLowerCase());
  }

  exact(p: string): string | undefined {
    return this.byPathLower.get(p.toLowerCase());
  }

  /** Anzahl Dateien mit gleichem Dateinamen (für "kürzester eindeutiger Link"). */
  nameCount(fileName: string): number {
    return this.byNameLower.get(fileName.toLowerCase())?.length ?? 0;
  }

  resolve(target: string, fromPath: string | null, kind: "wikilink" | "markdown" = "wikilink"): string | null {
    if (!target) return fromPath; // [[#Überschrift]] = Link auf sich selbst
    const t = target.replace(/\\/g, "/");
    const fromDir = fromPath ? path.posix.dirname(fromPath) : ".";
    const variants = path.posix.extname(t) && TEXT_EXTENSIONS.has(path.posix.extname(t).toLowerCase()) ? [t] : [t, `${t}.md`];

    for (const v of variants) {
      const explicitRelative = v.startsWith("./") || v.startsWith("../");
      // 1) relativ zur Quelldatei (explizit ./ ../ oder Markdown-Links)
      if (explicitRelative || kind === "markdown") {
        const joined = path.posix.normalize(path.posix.join(fromDir, v));
        if (!joined.startsWith("..")) {
          const hit = this.exact(joined);
          if (hit) return hit;
        }
        if (explicitRelative) continue;
      }
      // 2) vault-absoluter Pfad
      const abs = path.posix.normalize(v.replace(/^\/+/, ""));
      const hit = this.exact(abs);
      if (hit) return hit;
    }

    // 3) Obsidian-Stil: Dateiname bzw. Pfad-Suffix irgendwo im Vault
    for (const v of variants) {
      const clean = path.posix.normalize(v.replace(/^\/+/, "")).toLowerCase();
      if (clean.startsWith("..")) continue;
      let candidates: string[];
      if (clean.includes("/")) {
        candidates = this.paths.filter((p) => p.toLowerCase().endsWith(`/${clean}`));
      } else {
        candidates = this.byNameLower.get(clean) ?? [];
      }
      if (candidates.length === 0) continue;
      if (candidates.length === 1) return candidates[0]!;
      const sameDir = candidates.find((c) => path.posix.dirname(c) === fromDir);
      if (sameDir) return sameDir;
      return [...candidates].sort((a, b) => a.split("/").length - b.split("/").length || a.length - b.length || a.localeCompare(b))[0]!;
    }
    return null;
  }
}

export class Vault {
  private cache = new Map<string, NoteInfo>();
  private indexPromise: Promise<VaultIndex> | null = null;

  private constructor(
    readonly name: string,
    readonly root: string,
    readonly readOnly: boolean,
    private readonly dailyOverride: DailyNotesOverride | undefined,
    readonly trashMode: "obsidian" | "none",
  ) {}

  static async open(cfg: VaultConfig, opts: { trash?: "obsidian" | "none" } = {}): Promise<Vault> {
    const real = await fs.realpath(cfg.path);
    return new Vault(cfg.name, real, cfg.readOnly, cfg.dailyNotes, opts.trash ?? "obsidian");
  }

  // ───────────────────────── Pfade ─────────────────────────

  async abs(rel: string): Promise<string> {
    return toAbsoluteSafe(this.root, rel);
  }

  assertWritable(): void {
    if (this.readOnly) throw new VaultError(`Vault "${this.name}" ist schreibgeschützt (read-only).`, "READ_ONLY");
  }

  // ───────────────────────── Dateisystem ─────────────────────────

  async walk(folder = ""): Promise<FileEntry[]> {
    const start = folder ? await this.abs(folder) : this.root;
    const out: FileEntry[] = [];
    const visit = async (dirAbs: string, dirRel: string): Promise<void> => {
      let entries: import("node:fs").Dirent[];
      try {
        entries = await fs.readdir(dirAbs, { withFileTypes: true });
      } catch (e) {
        if ((e as NodeJS.ErrnoException).code === "ENOENT") return;
        throw e;
      }
      await Promise.all(
        entries.map(async (ent) => {
          if (ent.name.startsWith(".")) return;
          const rel = dirRel ? `${dirRel}/${ent.name}` : ent.name;
          const full = path.join(dirAbs, ent.name);
          if (ent.isDirectory()) {
            if (IGNORED_DIRS.has(ent.name)) return;
            await visit(full, rel);
          } else if (ent.isFile()) {
            try {
              const st = await fs.stat(full);
              out.push({ path: rel, mtimeMs: st.mtimeMs, ctimeMs: st.birthtimeMs || st.ctimeMs, size: st.size, isNote: isNotePath(rel) });
            } catch {
              /* Datei zwischenzeitlich gelöscht */
            }
          }
          // Symlinks werden bewusst ignoriert (Sicherheit).
        }),
      );
    };
    await visit(start, folder);
    out.sort((a, b) => a.path.localeCompare(b.path));
    return out;
  }

  async listFolders(folder = ""): Promise<string[]> {
    const start = folder ? await this.abs(folder) : this.root;
    const out: string[] = [];
    const visit = async (dirAbs: string, dirRel: string): Promise<void> => {
      let entries: import("node:fs").Dirent[];
      try {
        entries = await fs.readdir(dirAbs, { withFileTypes: true });
      } catch {
        return;
      }
      for (const ent of entries) {
        if (!ent.isDirectory() || ent.name.startsWith(".") || IGNORED_DIRS.has(ent.name)) continue;
        const rel = dirRel ? `${dirRel}/${ent.name}` : ent.name;
        out.push(rel);
        await visit(path.join(dirAbs, ent.name), rel);
      }
    };
    await visit(start, folder);
    return out.sort((a, b) => a.localeCompare(b));
  }

  async exists(rel: string): Promise<boolean> {
    try {
      await fs.access(await this.abs(rel));
      return true;
    } catch (e) {
      if (e instanceof VaultError) throw e;
      return false;
    }
  }

  async isFile(rel: string): Promise<boolean> {
    try {
      return (await fs.stat(await this.abs(rel))).isFile();
    } catch (e) {
      if (e instanceof VaultError) throw e;
      return false;
    }
  }

  async readFile(rel: string): Promise<string> {
    const ext = path.posix.extname(rel).toLowerCase();
    if (!TEXT_EXTENSIONS.has(ext)) {
      throw new VaultError(`Nur Textdateien (${[...TEXT_EXTENSIONS].join(", ")}) können gelesen werden: ${rel}`, "INVALID");
    }
    try {
      return await fs.readFile(await this.abs(rel), "utf8");
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code === "ENOENT") throw new VaultError(`Datei nicht gefunden: ${rel}`, "NOT_FOUND");
      throw e;
    }
  }

  /** Atomares Schreiben (temp + rename), legt fehlende Ordner an. */
  async writeFile(rel: string, content: string, opts: { overwrite: boolean }): Promise<{ created: boolean }> {
    this.assertWritable();
    const ext = path.posix.extname(rel).toLowerCase();
    if (!TEXT_EXTENSIONS.has(ext)) throw new VaultError(`Nur Textdateien können geschrieben werden: ${rel}`, "INVALID");
    const abs = await this.abs(rel);
    const existed = await this.exists(rel);
    if (existed && !opts.overwrite) throw new VaultError(`Datei existiert bereits: ${rel} (overwrite=true zum Überschreiben)`, "EXISTS");
    await fs.mkdir(path.dirname(abs), { recursive: true });
    const tmp = path.join(path.dirname(abs), `.${path.basename(abs)}.${randomBytes(4).toString("hex")}.tmp`);
    await fs.writeFile(tmp, content, "utf8");
    try {
      await fs.rename(tmp, abs);
    } catch (e) {
      await fs.rm(tmp, { force: true });
      throw e;
    }
    this.invalidate();
    return { created: !existed };
  }

  async moveFile(from: string, to: string, opts: { overwrite: boolean }): Promise<void> {
    this.assertWritable();
    const fromAbs = await this.abs(from);
    const toAbs = await this.abs(to);
    if (!(await this.isFile(from))) throw new VaultError(`Datei nicht gefunden: ${from}`, "NOT_FOUND");
    if (from.toLowerCase() !== to.toLowerCase() && (await this.exists(to)) && !opts.overwrite) {
      throw new VaultError(`Ziel existiert bereits: ${to}`, "EXISTS");
    }
    await fs.mkdir(path.dirname(toAbs), { recursive: true });
    await fs.rename(fromAbs, toAbs);
    this.invalidate();
  }

  /** Verschiebt in <vault>/.trash (wie Obsidian) oder löscht endgültig. Liefert den Papierkorb-Pfad. */
  async deleteFile(rel: string, permanent: boolean): Promise<string | null> {
    this.assertWritable();
    const abs = await this.abs(rel);
    if (!(await this.isFile(rel))) throw new VaultError(`Datei nicht gefunden: ${rel}`, "NOT_FOUND");
    if (permanent) {
      await fs.rm(abs);
      this.invalidate();
      return null;
    }
    if (this.trashMode === "none") {
      throw new VaultError("Papierkorb ist deaktiviert – zum Löschen permanent=true setzen.", "INVALID");
    }
    const trashDir = path.join(this.root, ".trash");
    await fs.mkdir(trashDir, { recursive: true });
    const ext = path.extname(abs);
    const stem = path.basename(abs, ext);
    let target = path.join(trashDir, `${stem}${ext}`);
    for (let i = 1; await fs.access(target).then(() => true, () => false); i++) {
      target = path.join(trashDir, `${stem} ${i}${ext}`);
    }
    await fs.rename(abs, target);
    this.invalidate();
    return `.trash/${path.basename(target)}`;
  }

  // ───────────────────────── Index ─────────────────────────

  invalidate(): void {
    this.indexPromise = null;
  }

  /**
   * Baut (bzw. aktualisiert) den Index. Dateien werden nur neu gelesen,
   * wenn sich mtime oder Größe geändert haben.
   */
  async index(): Promise<VaultIndex> {
    // Kurzlebiges Promise-Sharing für parallele Tool-Aufrufe; jede neue Anfrage prüft mtimes erneut.
    if (this.indexPromise) return this.indexPromise;
    const p = this.buildIndex().finally(() => {
      setTimeout(() => {
        if (this.indexPromise === p) this.indexPromise = null;
      }, 250).unref?.();
    });
    this.indexPromise = p;
    return p;
  }

  private async buildIndex(): Promise<VaultIndex> {
    const files = await this.walk();
    const notes: NoteInfo[] = [];
    const seen = new Set<string>();
    const todo = files.filter((f) => f.isNote);
    const CONCURRENCY = 64;
    for (let i = 0; i < todo.length; i += CONCURRENCY) {
      const batch = todo.slice(i, i + CONCURRENCY);
      const infos = await Promise.all(
        batch.map(async (f) => {
          seen.add(f.path);
          const cached = this.cache.get(f.path);
          if (cached && cached.mtimeMs === f.mtimeMs && cached.size === f.size) return cached;
          let content: string;
          try {
            content = await fs.readFile(path.join(this.root, ...f.path.split("/")), "utf8");
          } catch {
            return null;
          }
          const info = this.parseNote(f, content);
          this.cache.set(f.path, info);
          return info;
        }),
      );
      for (const info of infos) if (info) notes.push(info);
    }
    for (const key of [...this.cache.keys()]) if (!seen.has(key)) this.cache.delete(key);
    return {
      files,
      notes,
      notesByPath: new Map(notes.map((n) => [n.path, n])),
      resolver: new LinkResolver(files.map((f) => f.path)),
    };
  }

  private parseNote(f: FileEntry, content: string): NoteInfo {
    const fm = parseFrontmatter(content);
    return {
      path: f.path,
      name: noteBasename(f.path),
      mtimeMs: f.mtimeMs,
      ctimeMs: f.ctimeMs,
      size: f.size,
      content,
      frontmatter: fm.data,
      frontmatterError: fm.error,
      tags: extractTags(content),
      links: extractLinks(content),
      headings: extractHeadings(content),
    };
  }

  /**
   * Findet eine existierende Datei anhand eines Pfads oder Notiznamens
   * ("Projekte/Idee.md", "Projekte/Idee", "Idee", "[[Idee]]").
   */
  async resolveExisting(ref: string): Promise<string> {
    const cleaned = ref.trim().replace(/^!?\[\[(.*)\]\]$/, "$1").replace(/[#|].*$/, "");
    const rel = normalizeVaultPath(cleaned);
    if (!rel) throw new VaultError("Pfad fehlt", "INVALID_PATH");
    if (await this.isFile(rel)) return rel;
    const withExt = ensureNoteExtension(rel);
    if (withExt !== rel && (await this.isFile(withExt))) return withExt;
    const idx = await this.index();
    const hit = idx.resolver.resolve(rel, null);
    if (hit) return hit;
    const needle = noteBasename(rel).toLowerCase();
    const suggestions = idx.notes
      .filter((n) => n.name.toLowerCase().includes(needle) || n.path.toLowerCase().includes(needle))
      .slice(0, 5)
      .map((n) => n.path);
    throw new VaultError(
      `Notiz nicht gefunden: ${ref}${suggestions.length ? ` – meintest du: ${suggestions.join(", ")}?` : ""}`,
      "NOT_FOUND",
    );
  }

  /** Pfad für eine neue Notiz: normalisiert und mit .md-Endung. */
  newNotePath(ref: string): string {
    const rel = normalizeVaultPath(ref);
    if (!rel) throw new VaultError("Pfad fehlt", "INVALID_PATH");
    return ensureNoteExtension(rel);
  }

  // ───────────────────────── Daily Notes ─────────────────────────

  async dailyNotesSettings(): Promise<DailyNotesSettings> {
    const readJson = async (rel: string): Promise<Record<string, unknown> | null> => {
      try {
        return JSON.parse(await fs.readFile(path.join(this.root, rel), "utf8")) as Record<string, unknown>;
      } catch {
        return null;
      }
    };
    const str = (v: unknown) => (typeof v === "string" && v.trim() ? v.trim() : undefined);
    let base: DailyNotesSettings = { folder: "", format: "YYYY-MM-DD", source: "default" };

    const core = await readJson(".obsidian/daily-notes.json");
    if (core) {
      base = {
        folder: str(core.folder) ?? "",
        format: str(core.format) ?? "YYYY-MM-DD",
        template: str(core.template),
        source: "daily-notes",
      };
    }
    const periodic = await readJson(".obsidian/plugins/periodic-notes/data.json");
    const daily = periodic?.daily as Record<string, unknown> | undefined;
    if (daily && daily.enabled === true) {
      base = {
        folder: str(daily.folder) ?? base.folder,
        format: str(daily.format) ?? base.format,
        template: str(daily.template) ?? base.template,
        source: "periodic-notes",
      };
    }
    if (this.dailyOverride) {
      base = {
        folder: this.dailyOverride.folder ?? base.folder,
        format: this.dailyOverride.format ?? base.format,
        template: this.dailyOverride.template ?? base.template,
        source: "config",
      };
    }
    base.folder = base.folder.replace(/^\/+|\/+$/g, "");
    return base;
  }

  // ───────────────────────── Link-Umschreibung ─────────────────────────

  /**
   * Schreibt Links in `content` um, deren Ziel `oldTarget` ist (bzw. bei Umzug der Quelldatei
   * relative Markdown-Links). Gibt null zurück, wenn nichts geändert wurde.
   */
  static rewriteLinks(args: {
    content: string;
    sourceOld: string;
    sourceNew: string;
    oldTarget: string;
    newTarget: string;
    before: LinkResolver;
    after: LinkResolver;
  }): { content: string; changed: number } | null {
    const { content, sourceOld, sourceNew, oldTarget, newTarget, before, after } = args;
    const links = extractLinks(content);
    const replacements: { offset: number; length: number; text: string }[] = [];
    const sourceMoved = sourceOld !== sourceNew;

    for (const link of links) {
      if (!link.target) continue; // Link auf eigene Überschrift
      const resolved = before.resolve(link.target, sourceOld, link.kind);
      if (!resolved) continue;
      const targetMoved = resolved === oldTarget;
      const finalTarget = targetMoved ? newTarget : resolved;

      if (link.kind === "wikilink") {
        if (!targetMoved) continue;
        const inner = link.raw.slice(link.embed ? 3 : 2, -2);
        const usesPath = link.target.includes("/");
        const oldExt = path.posix.extname(oldTarget).toLowerCase();
        const keepExt = oldExt !== "" && link.target.toLowerCase().endsWith(oldExt);
        const fileName = path.posix.basename(finalTarget);
        let text: string;
        if (!usesPath && after.nameCount(fileName) <= 1) {
          text = keepExt ? fileName : noteBasename(finalTarget);
        } else {
          text = keepExt ? finalTarget : stripNoteExt(finalTarget);
        }
        const escIdx = inner.indexOf("\\|");
        const pipeIdx = escIdx >= 0 ? escIdx : inner.indexOf("|");
        const aliasPart = pipeIdx >= 0 ? inner.slice(pipeIdx) : "";
        const newRaw = `${link.embed ? "!" : ""}[[${text}${link.subpath ?? ""}${aliasPart}]]`;
        if (newRaw !== link.raw) replacements.push({ offset: link.offset, length: link.raw.length, text: newRaw });
      } else {
        if (!targetMoved && !sourceMoved) continue;
        const hrefRaw = link.hrefRaw!;
        const angle = hrefRaw.startsWith("<");
        const inner = angle ? hrefRaw.slice(1, -1) : hrefRaw;
        const hashIdx = inner.indexOf("#");
        const suffix = hashIdx >= 0 ? inner.slice(hashIdx) : "";
        const pathPart = hashIdx >= 0 ? inner.slice(0, hashIdx) : inner;
        let decoded = pathPart;
        try {
          decoded = decodeURIComponent(pathPart);
        } catch {
          /* egal */
        }
        // War der Link relativ zur Quelldatei oder vault-absolut?
        const relFromOld = path.posix.normalize(path.posix.join(path.posix.dirname(sourceOld), decoded));
        const wasRelative = !decoded.startsWith("/") && before.exact(relFromOld) === resolved;
        if (!wasRelative && !targetMoved) continue; // vault-absolute Links bleiben bei Quell-Umzug gültig
        let newPath = wasRelative ? path.posix.relative(path.posix.dirname(sourceNew), finalTarget) : finalTarget;
        if (wasRelative && decoded.startsWith("./") && !newPath.startsWith(".")) newPath = `./${newPath}`;
        if (decoded.startsWith("/") && !wasRelative) newPath = `/${newPath}`;
        const encoded = angle ? newPath : newPath.replace(/ /g, "%20");
        const newHref = angle ? `<${encoded}${suffix}>` : `${encoded}${suffix}`;
        if (newHref === hrefRaw) continue;
        const hrefPos = link.raw.indexOf(hrefRaw, link.raw.indexOf("]("));
        const newRaw = link.raw.slice(0, hrefPos) + newHref + link.raw.slice(hrefPos + hrefRaw.length);
        replacements.push({ offset: link.offset, length: link.raw.length, text: newRaw });
      }
    }
    if (replacements.length === 0) return null;
    let out = content;
    for (const r of replacements.sort((a, b) => b.offset - a.offset)) {
      out = out.slice(0, r.offset) + r.text + out.slice(r.offset + r.length);
    }
    return { content: out, changed: replacements.length };
  }
}

export class VaultRegistry {
  private constructor(
    private readonly vaults: Map<string, Vault>,
    readonly defaultName: string,
    readonly locale: string,
  ) {}

  static async create(configs: VaultConfig[], opts: { defaultVault: string; locale: string; trash?: "obsidian" | "none" }): Promise<VaultRegistry> {
    const map = new Map<string, Vault>();
    for (const cfg of configs) map.set(cfg.name, await Vault.open(cfg, { trash: opts.trash }));
    return new VaultRegistry(map, opts.defaultVault, opts.locale);
  }

  get(name?: string): Vault {
    const key = name?.trim() || this.defaultName;
    const v = this.vaults.get(key) ?? this.vaults.get(key.toLowerCase());
    if (!v) throw new VaultError(`Unbekannter Vault "${name}". Verfügbar: ${this.names().join(", ")}`, "NOT_FOUND");
    return v;
  }

  names(): string[] {
    return [...this.vaults.keys()];
  }

  all(): Vault[] {
    return [...this.vaults.values()];
  }

  get anyWritable(): boolean {
    return this.all().some((v) => !v.readOnly);
  }
}
