import YAML from "yaml";

export type Frontmatter = Record<string, unknown>;

export interface ParsedFrontmatter {
  /** Geparstes Frontmatter oder null, wenn keins vorhanden ist. */
  data: Frontmatter | null;
  /** Fehlermeldung, falls der YAML-Block kaputt ist. */
  error?: string;
  /** Rohtext zwischen den --- Markern. */
  raw: string;
  /** Inhalt nach dem Frontmatter. */
  body: string;
  /** Zeichen-Offset, an dem der Body beginnt. */
  bodyOffset: number;
}

const FM_RE = /^---[ \t]*\r?\n([\s\S]*?)(?:\r?\n)?^(?:---|\.\.\.)[ \t]*(?:\r?\n|$)/m;

export function parseFrontmatter(content: string): ParsedFrontmatter {
  const text = content.startsWith("﻿") ? content.slice(1) : content;
  const bom = content.length - text.length;
  if (!/^---[ \t]*\r?\n/.test(text)) {
    return { data: null, raw: "", body: content, bodyOffset: 0 };
  }
  const m = FM_RE.exec(text);
  if (!m || m.index !== 0) {
    return { data: null, raw: "", body: content, bodyOffset: 0 };
  }
  const raw = m[1] ?? "";
  const bodyOffset = bom + m[0].length;
  const body = content.slice(bodyOffset);
  if (raw.trim() === "") return { data: {}, raw, body, bodyOffset };
  try {
    const parsed = YAML.parse(raw, { prettyErrors: true, logLevel: "error" });
    if (parsed === null || parsed === undefined) return { data: {}, raw, body, bodyOffset };
    if (typeof parsed !== "object" || Array.isArray(parsed)) {
      return { data: null, error: "Frontmatter ist kein YAML-Objekt", raw, body, bodyOffset };
    }
    return { data: parsed as Frontmatter, raw, body, bodyOffset };
  } catch (e) {
    return { data: null, error: (e as Error).message.split("\n")[0], raw, body, bodyOffset };
  }
}

export function stringifyFrontmatter(data: Frontmatter): string {
  if (Object.keys(data).length === 0) return "";
  const yaml = YAML.stringify(data, { lineWidth: 0 });
  return `---\n${yaml}${yaml.endsWith("\n") ? "" : "\n"}---\n`;
}

/** Setzt Frontmatter + Body wieder zusammen. */
export function composeNote(data: Frontmatter | null, body: string): string {
  if (!data || Object.keys(data).length === 0) return body;
  return stringifyFrontmatter(data) + body;
}

/**
 * Ersetzt Code-Blöcke, Inline-Code, HTML- und Obsidian-Kommentare (%% … %%)
 * durch Leerzeichen, ohne Zeilenumbrüche oder Offsets zu verändern.
 * So können Links/Tags per Regex gesucht werden, ohne Code zu treffen.
 */
export function maskNonContent(text: string): string {
  const blank = (s: string) => s.replace(/[^\n]/g, " ");
  let out = text;
  // Fenced code blocks (``` oder ~~~), auch unbeendet bis Dateiende
  out = out.replace(/^([ \t]*)(`{3,}|~{3,})[^\n]*\n[\s\S]*?(?:^\1?\2[ \t]*$|(?![\s\S]))/gm, blank);
  // Obsidian-Kommentare und HTML-Kommentare
  out = out.replace(/%%[\s\S]*?(?:%%|(?![\s\S]))/g, blank);
  out = out.replace(/<!--[\s\S]*?(?:-->|(?![\s\S]))/g, blank);
  // Inline-Code (auch mit mehreren Backticks)
  out = out.replace(/(`+)(?!`)(.+?)(?<!`)\1(?!`)/g, blank);
  // Math-Blöcke
  out = out.replace(/\$\$[\s\S]*?\$\$/g, blank);
  return out;
}

export interface LinkRef {
  /** Originaltext des Links, z. B. "[[Notiz#Kapitel|Alias]]" */
  raw: string;
  /** Linkziel ohne Unterpfad, z. B. "Ordner/Notiz" */
  target: string;
  /** "#Überschrift" oder "#^block-id", falls vorhanden */
  subpath?: string;
  alias?: string;
  embed: boolean;
  kind: "wikilink" | "markdown";
  /** Nur bei Markdown-Links: das rohe href-Token, wie es in der Datei steht (inkl. <…> und #…) */
  hrefRaw?: string;
  /** 1-basierte Zeilennummer */
  line: number;
  /** Zeichen-Offset in der Datei */
  offset: number;
  inFrontmatter: boolean;
}

const WIKILINK_RE = /(!?)\[\[([^\[\]\n]+?)\]\]/g;
const MDLINK_RE = /(!?)\[((?:\\.|[^\]\\\n])*)\]\(\s*(<[^>\n]+>|[^)\s]+)(?:\s+(?:"[^"\n]*"|'[^'\n]*'))?\s*\)/g;
const EXTERNAL_RE = /^[a-z][a-z0-9+.-]*:/i;

function lineAt(text: string, offset: number, cache: number[]): number {
  // cache = Offsets der Zeilenanfänge
  let lo = 0;
  let hi = cache.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (cache[mid]! <= offset) lo = mid;
    else hi = mid - 1;
  }
  return lo + 1;
}

function lineStarts(text: string): number[] {
  const starts = [0];
  for (let i = 0; i < text.length; i++) if (text.charCodeAt(i) === 10) starts.push(i + 1);
  return starts;
}

export function parseWikiInner(inner: string): { target: string; subpath?: string; alias?: string } {
  // In Tabellen wird die Pipe als \| geschrieben
  const cleaned = inner.replace(/\\\|/g, "|");
  const pipe = cleaned.indexOf("|");
  const linkPart = pipe >= 0 ? cleaned.slice(0, pipe) : cleaned;
  const alias = pipe >= 0 ? cleaned.slice(pipe + 1).trim() : undefined;
  const hash = linkPart.indexOf("#");
  const target = (hash >= 0 ? linkPart.slice(0, hash) : linkPart).trim();
  const subpath = hash >= 0 ? linkPart.slice(hash).trim() : undefined;
  return { target, subpath: subpath || undefined, alias: alias || undefined };
}

function safeDecode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

/** Findet alle internen Links (Wikilinks, Embeds, Markdown-Links) in einer Notiz. */
export function extractLinks(content: string): LinkRef[] {
  const fm = parseFrontmatter(content);
  const masked = content.slice(0, fm.bodyOffset) + maskNonContent(content.slice(fm.bodyOffset));
  const starts = lineStarts(content);
  const links: LinkRef[] = [];

  for (const m of masked.matchAll(WIKILINK_RE)) {
    const offset = m.index!;
    const raw = content.slice(offset, offset + m[0].length);
    const inner = raw.slice(m[1]!.length + 2, -2);
    const { target, subpath, alias } = parseWikiInner(inner);
    if (!target && !subpath) continue;
    links.push({
      raw,
      target,
      subpath,
      alias,
      embed: m[1] === "!",
      kind: "wikilink",
      line: lineAt(content, offset, starts),
      offset,
      inFrontmatter: offset < fm.bodyOffset,
    });
  }

  const bodyMasked = masked.slice(fm.bodyOffset);
  for (const m of bodyMasked.matchAll(MDLINK_RE)) {
    const offset = fm.bodyOffset + m.index!;
    const raw = content.slice(offset, offset + m[0].length);
    const hrefRaw = m[3]!;
    let href = hrefRaw;
    if (href.startsWith("<") && href.endsWith(">")) href = href.slice(1, -1);
    if (EXTERNAL_RE.test(href)) continue;
    const hash = href.indexOf("#");
    const targetRaw = hash >= 0 ? href.slice(0, hash) : href;
    const subpath = hash >= 0 ? safeDecode(href.slice(hash)) : undefined;
    const target = safeDecode(targetRaw).trim();
    if (!target && !subpath) continue;
    links.push({
      raw,
      target,
      subpath,
      alias: m[2] || undefined,
      embed: m[1] === "!",
      kind: "markdown",
      hrefRaw,
      line: lineAt(content, offset, starts),
      offset,
      inFrontmatter: false,
    });
  }

  links.sort((a, b) => a.offset - b.offset);
  return links;
}

// Obsidian-Tags: Buchstaben, Ziffern, _, -, / (verschachtelt); rein numerisch ist kein Tag.
const INLINE_TAG_RE = /(^|[\s(\[{,;])#([\p{L}\p{N}_\-/]*[\p{L}_\-/][\p{L}\p{N}_\-/]*)/gu;

function normalizeTag(tag: string): string {
  return tag.trim().replace(/^#+/, "").replace(/\/+$/, "");
}

export function frontmatterTags(data: Frontmatter | null): string[] {
  if (!data) return [];
  const out: string[] = [];
  for (const key of ["tags", "tag", "Tags", "Tag"]) {
    const v = data[key];
    if (v === undefined || v === null) continue;
    const items = Array.isArray(v) ? v : String(v).split(/[,\s]+/);
    for (const item of items) {
      if (item === null || item === undefined) continue;
      const t = normalizeTag(String(item));
      if (t) out.push(t);
    }
  }
  return out;
}

export function inlineTags(body: string): string[] {
  const masked = maskNonContent(body);
  const out: string[] = [];
  for (const m of masked.matchAll(INLINE_TAG_RE)) {
    const t = normalizeTag(m[2]!);
    if (t) out.push(t);
  }
  return out;
}

/** Alle Tags (Frontmatter + Inline), eindeutig und case-insensitiv dedupliziert. */
export function extractTags(content: string): string[] {
  const fm = parseFrontmatter(content);
  const seen = new Map<string, string>();
  for (const t of [...frontmatterTags(fm.data), ...inlineTags(fm.body)]) {
    const key = t.toLowerCase();
    if (!seen.has(key)) seen.set(key, t);
  }
  return [...seen.values()];
}

/** true, wenn `tag` gleich `query` ist oder (bei includeNested) ein Untertag davon. */
export function tagMatches(tag: string, query: string, includeNested = true): boolean {
  const t = tag.toLowerCase();
  const q = normalizeTag(query).toLowerCase();
  return t === q || (includeNested && t.startsWith(`${q}/`));
}

export interface Heading {
  level: number;
  text: string;
  line: number;
}

export function extractHeadings(content: string): Heading[] {
  const fm = parseFrontmatter(content);
  const masked = content.slice(0, fm.bodyOffset).replace(/[^\n]/g, " ") + maskNonContent(content.slice(fm.bodyOffset));
  const lines = masked.split("\n");
  const orig = content.split("\n");
  const headings: Heading[] = [];
  lines.forEach((line, i) => {
    const m = /^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*\r?$/.exec(line);
    if (m) {
      const o = /^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*\r?$/.exec(orig[i]!);
      headings.push({ level: m[1]!.length, text: (o?.[2] ?? m[2]!).trim(), line: i + 1 });
    }
  });
  return headings;
}

function headingKey(s: string): string {
  return s.replace(/^#+\s*/, "").trim().toLowerCase();
}

export interface SectionRange {
  heading: Heading;
  /** Zeichen-Offset direkt nach der Überschriftenzeile */
  contentStart: number;
  /** Zeichen-Offset, an dem der nächste gleich- oder höherrangige Abschnitt beginnt (bzw. Dateiende) */
  end: number;
}

/**
 * Findet einen Abschnitt anhand einer Überschrift. Unterstützt verschachtelte Pfade
 * wie "Projekte#Aktiv" oder "Projekte > Aktiv".
 */
export function findSection(content: string, headingPath: string): SectionRange | null {
  const parts = headingPath
    .split(/\s*(?:#|>)\s*/)
    .map(headingKey)
    .filter(Boolean);
  if (parts.length === 0) return null;
  const headings = extractHeadings(content);
  const starts = lineStarts(content);
  const offsetOfLine = (line: number) => (line - 1 < starts.length ? starts[line - 1]! : content.length);

  let searchFrom = 0;
  let parentLevel = 0;
  let parentEndLine = Infinity;
  let found: Heading | null = null;
  for (const part of parts) {
    found = null;
    for (let i = searchFrom; i < headings.length; i++) {
      const h = headings[i]!;
      if (h.line > parentEndLine) break;
      if (h.level > parentLevel && headingKey(h.text) === part) {
        found = h;
        searchFrom = i + 1;
        break;
      }
    }
    if (!found) return null;
    parentLevel = found.level;
    const next = headings.find((h) => h.line > found!.line && h.level <= found!.level);
    parentEndLine = next ? next.line - 1 : Infinity;
  }
  if (!found) return null;
  const next = headings.find((h) => h.line > found!.line && h.level <= found!.level);
  const contentStart = offsetOfLine(found.line + 1);
  const end = next ? offsetOfLine(next.line) : content.length;
  return { heading: found, contentStart: Math.min(contentStart, content.length), end };
}

/** Kurzer Textausschnitt um eine Zeile herum. */
export function contextLines(lines: string[], lineIndex: number, radius: number): string {
  const from = Math.max(0, lineIndex - radius);
  const to = Math.min(lines.length, lineIndex + radius + 1);
  return lines.slice(from, to).join("\n");
}
