import { composeNote, extractHeadings, findSection, frontmatterTags, parseFrontmatter, type Frontmatter } from "./markdown.js";
import { VaultError } from "./paths.js";

function withTrailingNewline(s: string): string {
  return s.endsWith("\n") ? s : `${s}\n`;
}

function headingNotFound(content: string, heading: string): VaultError {
  const available = extractHeadings(content)
    .map((h) => `${"#".repeat(h.level)} ${h.text}`)
    .slice(0, 30);
  return new VaultError(
    `Überschrift "${heading}" nicht gefunden.${available.length ? ` Vorhanden: ${available.join(" | ")}` : " Die Notiz hat keine Überschriften."}`,
    "NOT_FOUND",
  );
}

/**
 * Fügt Text am Anfang/Ende der Notiz oder eines Abschnitts ein.
 * "start" ohne Überschrift = direkt nach dem Frontmatter.
 */
export function insertContent(
  content: string,
  addition: string,
  opts: { heading?: string; position?: "start" | "end"; createHeading?: boolean } = {},
): string {
  const position = opts.position ?? "end";
  const add = withTrailingNewline(addition);

  if (opts.heading) {
    const section = findSection(content, opts.heading);
    if (!section) {
      if (!opts.createHeading) throw headingNotFound(content, opts.heading);
      const title = opts.heading.split(/\s*(?:#|>)\s*/).filter(Boolean).pop()!.replace(/^#+\s*/, "");
      const base = content.length === 0 ? "" : content.endsWith("\n\n") ? content : content.endsWith("\n") ? `${content}\n` : `${content}\n\n`;
      return `${base}## ${title}\n${add}`;
    }
    if (position === "start") {
      const needsNl = section.contentStart > 0 && content[section.contentStart - 1] !== "\n";
      return content.slice(0, section.contentStart) + (needsNl ? "\n" : "") + add + content.slice(section.contentStart);
    }
    const sectionText = content.slice(section.contentStart, section.end);
    const trimmedLen = sectionText.replace(/\s+$/, "").length;
    if (trimmedLen === 0) {
      const needsNl = section.contentStart > 0 && content[section.contentStart - 1] !== "\n";
      // Leerer Abschnitt: direkt unter die Überschrift, Leerzeilen danach erhalten
      return content.slice(0, section.contentStart) + (needsNl ? "\n" : "") + add + content.slice(section.contentStart);
    }
    const insertPos = section.contentStart + trimmedLen;
    return `${content.slice(0, insertPos)}\n${add.replace(/\n$/, "")}${content.slice(insertPos)}` + (insertPos === content.length ? "\n" : "");
  }

  if (position === "start") {
    const fm = parseFrontmatter(content);
    return content.slice(0, fm.bodyOffset) + add + content.slice(fm.bodyOffset);
  }
  if (content.length === 0) return add;
  return withTrailingNewline(content) + add;
}

/** Ersetzt den Inhalt unter einer Überschrift (die Überschrift selbst bleibt). */
export function replaceSection(content: string, heading: string, newBody: string): string {
  const section = findSection(content, heading);
  if (!section) throw headingNotFound(content, heading);
  const isLast = section.end >= content.length;
  let body = withTrailingNewline(newBody);
  if (!isLast) body += "\n"; // Leerzeile vor der nächsten Überschrift
  const needsNl = section.contentStart > 0 && content[section.contentStart - 1] !== "\n";
  return content.slice(0, section.contentStart) + (needsNl ? "\n" : "") + body + content.slice(section.end);
}

export interface TextEdit {
  oldText: string;
  newText: string;
  replaceAll?: boolean;
}

export function applyEdits(content: string, edits: TextEdit[]): { content: string; replacements: number } {
  let out = content;
  let total = 0;
  edits.forEach((edit, i) => {
    if (!edit.oldText) throw new VaultError(`Edit ${i + 1}: oldText darf nicht leer sein`, "INVALID");
    const count = out.split(edit.oldText).length - 1;
    if (count === 0) {
      throw new VaultError(`Edit ${i + 1}: oldText nicht gefunden (Whitespace/Zeilenumbrüche exakt prüfen): ${JSON.stringify(edit.oldText.slice(0, 120))}`, "NOT_FOUND");
    }
    if (count > 1 && !edit.replaceAll) {
      throw new VaultError(`Edit ${i + 1}: oldText kommt ${count}× vor – mehr Kontext angeben oder replaceAll=true setzen`, "AMBIGUOUS");
    }
    out = edit.replaceAll ? out.split(edit.oldText).join(edit.newText) : out.replace(edit.oldText, () => edit.newText);
    total += edit.replaceAll ? count : 1;
  });
  return { content: out, replacements: total };
}

export interface FrontmatterUpdate {
  set?: Record<string, unknown>;
  remove?: string[];
  addTags?: string[];
  removeTags?: string[];
}

export function updateFrontmatter(content: string, update: FrontmatterUpdate): { content: string; frontmatter: Frontmatter } {
  const fm = parseFrontmatter(content);
  if (fm.error) throw new VaultError(`Frontmatter ist fehlerhaft und wird nicht angefasst: ${fm.error}`, "INVALID");
  const data: Frontmatter = { ...(fm.data ?? {}) };

  for (const [k, v] of Object.entries(update.set ?? {})) {
    if (v === null) delete data[k];
    else data[k] = v;
  }
  for (const k of update.remove ?? []) delete data[k];

  if (update.addTags?.length || update.removeTags?.length) {
    const current = frontmatterTags({ tags: data.tags ?? data.tag });
    const lower = new Set(current.map((t) => t.toLowerCase()));
    const clean = (t: string) => t.trim().replace(/^#+/, "");
    for (const t of (update.addTags ?? []).map(clean).filter(Boolean)) {
      if (!lower.has(t.toLowerCase())) {
        current.push(t);
        lower.add(t.toLowerCase());
      }
    }
    const remove = new Set((update.removeTags ?? []).map((t) => clean(t).toLowerCase()));
    const next = current.filter((t) => !remove.has(t.toLowerCase()));
    delete data.tag;
    if (next.length) data.tags = next;
    else delete data.tags;
  }

  return { content: composeNote(data, fm.body), frontmatter: data };
}

/** Kombiniert neuen Inhalt mit zusätzlichem Frontmatter (übergebene Werte gewinnen). */
export function withFrontmatter(content: string, extra: Record<string, unknown> | undefined): string {
  if (!extra || Object.keys(extra).length === 0) return content;
  const fm = parseFrontmatter(content);
  if (fm.error) throw new VaultError(`Frontmatter im Inhalt ist fehlerhaft: ${fm.error}`, "INVALID");
  return composeNote({ ...(fm.data ?? {}), ...extra }, fm.body);
}
