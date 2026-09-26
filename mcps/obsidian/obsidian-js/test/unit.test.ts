import { describe, expect, it } from "vitest";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { loadConfig, parseArgs, slugifyVaultName } from "../src/config.js";
import { formatDate, parseDateInput, renderTemplate } from "../src/vault/dates.js";
import { applyEdits, insertContent, replaceSection, updateFrontmatter } from "../src/vault/edit.js";
import { extractHeadings, extractLinks, extractTags, findSection, maskNonContent, parseFrontmatter, tagMatches } from "../src/vault/markdown.js";
import { normalizeVaultPath } from "../src/vault/paths.js";
import { LinkResolver, Vault } from "../src/vault/vault.js";
import { makeVault } from "./helpers.js";

describe("frontmatter", () => {
  it("parst YAML und Body", () => {
    const fm = parseFrontmatter("---\ntitle: Hallo\ntags: [a, b]\n---\n# Body\n");
    expect(fm.data).toEqual({ title: "Hallo", tags: ["a", "b"] });
    expect(fm.body).toBe("# Body\n");
  });
  it("ohne Frontmatter", () => {
    const fm = parseFrontmatter("# Nur Text\n---\nfoo");
    expect(fm.data).toBeNull();
    expect(fm.bodyOffset).toBe(0);
  });
  it("Template-Platzhalter erzeugen keine Warnungen auf stderr", () => {
    const warn = process.emitWarning;
    let warned = false;
    process.emitWarning = (() => {
      warned = true;
    }) as typeof process.emitWarning;
    try {
      parseFrontmatter("---\ntitle: {{title}}\n---\n");
    } finally {
      process.emitWarning = warn;
    }
    expect(warned).toBe(false);
  });
  it("leeres Frontmatter", () => {
    expect(parseFrontmatter("---\n---\ntext").data).toEqual({});
  });
  it("kaputtes YAML wird gemeldet", () => {
    const fm = parseFrontmatter("---\nfoo: [unclosed\n---\ntext");
    expect(fm.data).toBeNull();
    expect(fm.error).toBeTruthy();
  });
});

describe("links", () => {
  const content = [
    "---",
    'related: "[[Projekt A]]"',
    "---",
    "Siehe [[Notiz]] und [[Ordner/Andere#Kapitel|Alias]] und ![[bild.png]].",
    "Markdown: [Text](../Ordner/Datei%20Eins.md#Abschnitt) und [Extern](https://example.com).",
    "`[[kein Link]]` im Code",
    "```",
    "[[auch kein Link]]",
    "```",
    "| Tabelle | [[Tab\\|Alias]] |",
    "Selbst: [[#Überschrift]]",
  ].join("\n");
  const links = extractLinks(content);

  it("findet Wikilinks, Embeds, Markdown-Links und Frontmatter-Links", () => {
    expect(links.map((l) => l.target)).toEqual(["Projekt A", "Notiz", "Ordner/Andere", "bild.png", "../Ordner/Datei Eins.md", "Tab", ""]);
  });
  it("Details", () => {
    const l = links.find((x) => x.target === "Ordner/Andere")!;
    expect(l.subpath).toBe("#Kapitel");
    expect(l.alias).toBe("Alias");
    expect(links.find((x) => x.target === "bild.png")!.embed).toBe(true);
    expect(links[0]!.inFrontmatter).toBe(true);
    expect(links.find((x) => x.kind === "markdown")!.subpath).toBe("#Abschnitt");
    expect(links.find((x) => x.target === "Tab")!.alias).toBe("Alias");
  });
  it("maskiert Code offsets-erhaltend", () => {
    const t = "a `x` b\n```\ncode\n```\nc";
    expect(maskNonContent(t).length).toBe(t.length);
    expect(maskNonContent(t).split("\n").length).toBe(t.split("\n").length);
  });
});

describe("tags", () => {
  it("Frontmatter + inline, ohne Code, Zahlen und Überschriften", () => {
    const c = "---\ntags: [projekt, '#Wichtig']\n---\n# Überschrift\nText #idee und #projekt/aktiv, #123 keine Zahl.\n`#code`\n```\n#nope\n```\n";
    expect(extractTags(c)).toEqual(["projekt", "Wichtig", "idee", "projekt/aktiv"]);
  });
  it("Tags als String", () => {
    expect(extractTags("---\ntags: a, b c\n---\n")).toEqual(["a", "b", "c"]);
  });
  it("tagMatches mit verschachtelten Tags", () => {
    expect(tagMatches("projekt/aktiv", "#projekt")).toBe(true);
    expect(tagMatches("projekt/aktiv", "projekt", false)).toBe(false);
    expect(tagMatches("Projekt", "projekt")).toBe(true);
    expect(tagMatches("projektx", "projekt")).toBe(false);
  });
});

describe("headings & sections", () => {
  const c = "# Titel\nIntro\n## Projekte\n### Aktiv\n- A\n### Archiv\n- Z\n## Log\n- x\n```\n# kein heading\n```\n";
  it("extrahiert Überschriften ohne Code", () => {
    expect(extractHeadings(c).map((h) => h.text)).toEqual(["Titel", "Projekte", "Aktiv", "Archiv", "Log"]);
  });
  it("findet verschachtelte Abschnitte", () => {
    const s = findSection(c, "Projekte#Aktiv")!;
    expect(c.slice(s.contentStart, s.end)).toBe("- A\n");
    const p = findSection(c, "Projekte")!;
    expect(c.slice(p.contentStart, p.end)).toBe("### Aktiv\n- A\n### Archiv\n- Z\n");
    expect(findSection(c, "Log > nix")).toBeNull();
  });
});

describe("edit", () => {
  it("insertContent am Ende eines Abschnitts", () => {
    const c = "# A\n- 1\n\n# B\n- 2\n";
    expect(insertContent(c, "- neu", { heading: "A" })).toBe("# A\n- 1\n- neu\n\n# B\n- 2\n");
    expect(insertContent(c, "- neu", { heading: "B" })).toBe("# A\n- 1\n\n# B\n- 2\n- neu\n");
    expect(insertContent(c, "- neu", { heading: "A", position: "start" })).toBe("# A\n- neu\n- 1\n\n# B\n- 2\n");
  });
  it("leerer Abschnitt und Überschrift als letzte Zeile", () => {
    expect(insertContent("# A\n\n# B\n", "x", { heading: "A" })).toBe("# A\nx\n\n# B\n");
    expect(insertContent("# A", "x", { heading: "A" })).toBe("# A\nx\n");
  });
  it("fehlende Überschrift anlegen oder Fehler", () => {
    expect(() => insertContent("# A\n", "x", { heading: "Z" })).toThrow(/nicht gefunden/);
    expect(insertContent("# A\n", "x", { heading: "Log", createHeading: true })).toBe("# A\n\n## Log\nx\n");
  });
  it("start ohne Überschrift respektiert Frontmatter", () => {
    expect(insertContent("---\na: 1\n---\nText\n", "Neu", { position: "start" })).toBe("---\na: 1\n---\nNeu\nText\n");
  });
  it("replaceSection", () => {
    expect(replaceSection("# A\nalt\n# B\nb\n", "A", "neu")).toBe("# A\nneu\n\n# B\nb\n");
    expect(replaceSection("# A\nalt\n", "A", "neu")).toBe("# A\nneu\n");
  });
  it("applyEdits prüft Eindeutigkeit", () => {
    expect(applyEdits("a b a", [{ oldText: "b", newText: "c" }]).content).toBe("a c a");
    expect(() => applyEdits("a b a", [{ oldText: "a", newText: "c" }])).toThrow(/2×/);
    expect(applyEdits("a b a", [{ oldText: "a", newText: "$&", replaceAll: true }]).content).toBe("$& b $&");
    expect(() => applyEdits("x", [{ oldText: "y", newText: "z" }])).toThrow(/nicht gefunden/);
  });
  it("updateFrontmatter mit Tags", () => {
    const r = updateFrontmatter("---\ntags: [a]\nx: 1\n---\nBody\n", { set: { status: "done", x: null }, addTags: ["#b", "A"], removeTags: ["a"] });
    expect(r.frontmatter).toEqual({ tags: ["b"], status: "done" });
    expect(r.content).toBe("---\ntags:\n  - b\nstatus: done\n---\nBody\n");
  });
});

describe("dates", () => {
  const d = new Date(2026, 8, 26, 14, 5, 9); // Samstag
  it("formatDate", () => {
    expect(formatDate(d, "YYYY-MM-DD")).toBe("2026-09-26");
    expect(formatDate(d, "YYYY/MM/DD dddd", "de")).toBe("2026/09/26 Samstag");
    expect(formatDate(d, "dddd, [der] Do MMMM YYYY", "de")).toBe("Samstag, der 26. September 2026");
    expect(formatDate(d, "ddd D MMM", "en")).toBe("Sat 26 Sep");
    expect(formatDate(d, "gggg-[W]ww")).toBe("2026-W39");
    expect(formatDate(d, "HH:mm:ss")).toBe("14:05:09");
    expect(formatDate(d, "Do", "en")).toBe("26th");
  });
  it("parseDateInput", () => {
    const now = new Date(2026, 8, 26, 10);
    expect(parseDateInput("heute", now).getDate()).toBe(26);
    expect(parseDateInput("gestern", now).getDate()).toBe(25);
    expect(parseDateInput("+7", now).getMonth()).toBe(9);
    expect(parseDateInput("2026-01-02", now).getFullYear()).toBe(2026);
    expect(() => parseDateInput("2026-02-31", now)).toThrow();
    expect(() => parseDateInput("irgendwann", now)).toThrow();
  });
  it("renderTemplate", () => {
    const out = renderTemplate("# {{title}}\n{{date:dddd}} {{time}} {{date}}", {
      date: new Date(2026, 8, 26),
      title: "2026-09-26",
      dateFormat: "YYYY-MM-DD",
      locale: "de",
      now: new Date(2020, 0, 1, 8, 30),
    });
    expect(out).toBe("# 2026-09-26\nSamstag 08:30 2026-09-26");
  });
});

describe("paths", () => {
  it("blockiert Traversal und versteckte Ordner", () => {
    expect(normalizeVaultPath("/Ordner//Notiz.md")).toBe("Ordner/Notiz.md");
    expect(normalizeVaultPath("a/../b.md")).toBe("b.md");
    expect(() => normalizeVaultPath("../x.md")).toThrow();
    expect(() => normalizeVaultPath("a/../../x.md")).toThrow();
    expect(() => normalizeVaultPath(".obsidian/app.json")).toThrow();
    expect(() => normalizeVaultPath("a/.git/config")).toThrow();
    expect(normalizeVaultPath("Ordner\\Notiz.md")).toBe("Ordner/Notiz.md");
  });
});

describe("LinkResolver", () => {
  const r = new LinkResolver(["Notiz.md", "A/Notiz.md", "A/B/Tief.md", "Bilder/bild.png", "Version 1.2.md", "A/Rel.md", "X/Doppelt.md", "A/Doppelt.md", "Z/Y/Doppelt.md"]);
  it("löst wie Obsidian auf", () => {
    expect(r.resolve("Notiz", null)).toBe("Notiz.md");
    expect(r.resolve("Notiz", "A/Rel.md")).toBe("Notiz.md"); // exakter Vault-Pfad gewinnt
    expect(r.resolve("Doppelt", "A/Rel.md")).toBe("A/Doppelt.md"); // gleicher Ordner bevorzugt
    expect(r.resolve("Doppelt", "Notiz.md")).toBe("A/Doppelt.md"); // sonst kürzester Pfad, dann alphabetisch
    expect(r.resolve("A/Notiz", null)).toBe("A/Notiz.md");
    expect(r.resolve("B/Tief", null)).toBe("A/B/Tief.md"); // Pfad-Suffix
    expect(r.resolve("tief", null)).toBe("A/B/Tief.md"); // case-insensitiv
    expect(r.resolve("bild.png", null)).toBe("Bilder/bild.png");
    expect(r.resolve("Version 1.2", null)).toBe("Version 1.2.md");
    expect(r.resolve("./B/Tief.md", "A/Rel.md", "markdown")).toBe("A/B/Tief.md");
    expect(r.resolve("../Notiz.md", "A/Rel.md", "markdown")).toBe("Notiz.md");
    expect(r.resolve("Gibtsnicht", null)).toBeNull();
  });
});

describe("rewriteLinks", () => {
  it("schreibt Wikilinks, Aliase, Embeds und Markdown-Links um", () => {
    const before = new LinkResolver(["Alt.md", "Quelle.md", "X/Andere.md"]);
    const after = new LinkResolver(["Neu/Name.md", "Quelle.md", "X/Andere.md"]);
    const content = "[[Alt]] [[Alt#H|Alias]] ![[Alt]] [m](Alt.md) [[Andere]] | [[Alt\\|T]] |";
    const res = Vault.rewriteLinks({ content, sourceOld: "Quelle.md", sourceNew: "Quelle.md", oldTarget: "Alt.md", newTarget: "Neu/Name.md", before, after })!;
    expect(res.content).toBe("[[Name]] [[Name#H|Alias]] ![[Name]] [m](Neu/Name.md) [[Andere]] | [[Name\\|T]] |");
    expect(res.changed).toBe(5);
  });
  it("aktualisiert relative Links der verschobenen Notiz selbst", () => {
    const before = new LinkResolver(["A/Ich.md", "A/Nachbar.md"]);
    const after = new LinkResolver(["B/C/Ich.md", "A/Nachbar.md"]);
    const res = Vault.rewriteLinks({
      content: "[n](Nachbar.md) [[Nachbar]]",
      sourceOld: "A/Ich.md",
      sourceNew: "B/C/Ich.md",
      oldTarget: "A/Ich.md",
      newTarget: "B/C/Ich.md",
      before,
      after,
    })!;
    expect(res.content).toBe("[n](../../A/Nachbar.md) [[Nachbar]]");
  });
  it("nutzt vollen Pfad, wenn der Name nicht eindeutig ist", () => {
    const before = new LinkResolver(["Alt.md", "Q.md", "Y/Name.md"]);
    const after = new LinkResolver(["Neu/Name.md", "Q.md", "Y/Name.md"]);
    const res = Vault.rewriteLinks({ content: "[[Alt]]", sourceOld: "Q.md", sourceNew: "Q.md", oldTarget: "Alt.md", newTarget: "Neu/Name.md", before, after })!;
    expect(res.content).toBe("[[Neu/Name]]");
  });
});

describe("config", () => {
  it("parseArgs", () => {
    const a = parseArgs(["--vault", "x=/tmp", "--http", "--port=4000", "/other", "--read-only"]);
    expect(a).toMatchObject({ vaults: ["x=/tmp", "/other"], http: true, port: 4000, readOnly: true });
  });
  it("slugify", () => {
    expect(slugifyVaultName("Example Vault Name")).toBe("example-vault-name");
    expect(slugifyVaultName("Über Ärger")).toBe("uber-arger");
  });
  it("loadConfig aus ENV und Datei", () => {
    const v = makeVault({ "a.md": "" });
    try {
      mkdirSync(path.join(v.root, "Zweiter"));
      const cfg = loadConfig([], { OBSIDIAN_VAULTS: `haupt=${v.root};${path.join(v.root, "Zweiter")}`, MCP_TRANSPORT: "http", MCP_AUTH_TOKEN: "t" });
      expect(cfg.vaults.map((x) => x.name)).toEqual(["haupt", "zweiter"]);
      expect(cfg.transport).toBe("http");
      expect(cfg.http.authToken).toBe("t");
      const file = path.join(v.root, "cfg.json");
      writeFileSync(file, JSON.stringify({ vaults: [{ name: "Mein Vault", path: v.root, readOnly: true, dailyNotes: { folder: "Daily" } }] }));
      const cfg2 = loadConfig(["--config", file], {});
      expect(cfg2.vaults[0]).toMatchObject({ name: "mein-vault", readOnly: true, dailyNotes: { folder: "Daily" } });
      expect(() => loadConfig([], {})).toThrow(/Kein Vault/);
      expect(() => loadConfig(["/gibt/es/nicht"], {})).toThrow(/existiert nicht/);
    } finally {
      v.cleanup();
    }
  });
});
