import { existsSync, readFileSync, symlinkSync, mkdtempSync, writeFileSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { formatDate } from "../src/vault/dates.js";
import { connect, makeVault } from "./helpers.js";

const FIXTURE: Record<string, string> = {
  "Start.md": "---\ntags: [index]\naliases: [Home]\n---\n# Start\nWillkommen. Siehe [[Projekte/Alpha]] und [[Beta|die Beta]].\n\n## Log\n- erster Eintrag\n",
  "Projekte/Alpha.md": "---\nstatus: aktiv\nprio: 2\ndue: 2026-10-01\ntags: [projekt/aktiv]\n---\n# Alpha\nDas Alpha-Projekt. Zurück zum [[Start]].\n![[diagramm.png]]\n[Beta relativ](../Beta.md)\n",
  "Beta.md": "---\nstatus: fertig\nprio: 5\ntags: projekt\n---\n# Beta\nBeta erwähnt Start ohne Link. #idee\n",
  "Notizen/Lose.md": "Keine Links hier, aber [[Fehlt]] und [[Fehlt]] und [[Auch weg]].\n",
  "Anhänge/diagramm.png": "PNG",
  "Anhänge/unbenutzt.pdf": "PDF",
  "Templates/Daily.md": "# {{title}}\nErstellt: {{date:dddd}}\n\n## Log\n",
  ".obsidian/daily-notes.json": JSON.stringify({ folder: "Journal", format: "YYYY/YYYY-MM-DD", template: "Templates/Daily" }),
  ".obsidian/app.json": "{\"secret\": true}",
};

describe("MCP End-to-End", () => {
  let vault: ReturnType<typeof makeVault>;
  let mcp: Awaited<ReturnType<typeof connect>>;

  beforeEach(async () => {
    vault = makeVault(FIXTURE);
    mcp = await connect([{ name: "test", path: vault.root, readOnly: false }]);
  });
  afterEach(async () => {
    await mcp.close();
    vault.cleanup();
  });

  const read = (rel: string) => readFileSync(path.join(vault.root, rel), "utf8");

  it("registriert alle Tools", async () => {
    const { tools } = await mcp.client.listTools();
    expect(tools.map((t) => t.name).sort()).toEqual(
      [
        "append_to_daily_note",
        "append_to_note",
        "delete_note",
        "edit_note",
        "find_orphans",
        "find_unresolved_links",
        "get_backlinks",
        "get_daily_note",
        "get_outgoing_links",
        "list_notes",
        "list_tags",
        "list_vaults",
        "move_note",
        "read_multiple_notes",
        "read_note",
        "recent_notes",
        "replace_section",
        "search_by_frontmatter",
        "search_by_tag",
        "search_notes",
        "update_frontmatter",
        "write_note",
      ].sort(),
    );
    expect(mcp.client.getInstructions()).toContain("Obsidian");
  });

  it("list_vaults und list_notes", async () => {
    const vaults = await mcp.callJson("list_vaults");
    expect(vaults[0]).toMatchObject({ name: "test", default: true, notes: 5, dailyNotes: { folder: "Journal", source: "daily-notes" } });
    const list = await mcp.callJson("list_notes", { includeFolders: true });
    expect(list.items.map((i: any) => i.path)).toEqual(["Beta.md", "Notizen/Lose.md", "Projekte/Alpha.md", "Start.md", "Templates/Daily.md"]);
    expect(list.folders).toContain("Projekte");
    const attach = await mcp.callJson("list_notes", { folder: "Anhänge", includeAttachments: true, recursive: false });
    expect(attach.total).toBe(2);
  });

  it("read_note per Name, Abschnitt und Metadaten", async () => {
    expect(await mcp.callText("read_note", { path: "Beta.md" })).toBe(FIXTURE["Beta.md"]);
    expect(await mcp.callText("read_note", { path: "alpha" })).toContain("(aufgelöster Pfad: Projekte/Alpha.md)");
    expect((await mcp.call("read_note", { path: "Start.md", section: "Log" })).content[0]!.text).toBe("- erster Eintrag\n");
    const res = await mcp.call("read_note", { path: "Projekte/Alpha", includeMetadata: true });
    const meta = JSON.parse(res.content[0]!.text);
    expect(meta.frontmatter.status).toBe("aktiv");
    expect(meta.tags).toEqual(["projekt/aktiv"]);
    expect(meta.outgoingLinks).toEqual(["Start.md", "Anhänge/diagramm.png", "Beta.md"]);
    const miss = await mcp.call("read_note", { path: "Alphaa" });
    expect(miss.isError).toBe(true);
    const multi = await mcp.callText("read_multiple_notes", { paths: ["Beta", "Gibtsnicht"] });
    expect(multi).toContain("===== Beta.md =====");
    expect(multi).toContain("[FEHLER]");
  });

  it("Sicherheit: kein Zugriff außerhalb/auf versteckte Pfade", async () => {
    for (const p of ["../etc/passwd", ".obsidian/app.json", "/../../x"]) {
      const r = await mcp.call("read_note", { path: p });
      expect(r.isError, p).toBe(true);
    }
    const w = await mcp.call("write_note", { path: "../../boese", content: "x" });
    expect(w.isError).toBe(true);
    const w2 = await mcp.call("write_note", { path: ".obsidian/plugins/x", content: "x" });
    expect(w2.isError).toBe(true);

    const outside = mkdtempSync(path.join(os.tmpdir(), "outside-"));
    writeFileSync(path.join(outside, "geheim.md"), "geheim");
    symlinkSync(outside, path.join(vault.root, "Link"));
    const r = await mcp.call("read_note", { path: "Link/geheim.md" });
    expect(r.isError).toBe(true);
    expect(r.content[0]!.text).toMatch(/Symlink/);
    const w3 = await mcp.call("write_note", { path: "Link/neu.md", content: "x" });
    expect(w3.isError).toBe(true);
    expect(existsSync(path.join(outside, "neu.md"))).toBe(false);
    rmSync(outside, { recursive: true, force: true });
  });

  it("write_note, append_to_note, edit_note, replace_section, update_frontmatter", async () => {
    const created = await mcp.callJson("write_note", { path: "Neu/Idee", content: "# Idee\nText\n", frontmatter: { tags: ["idee"] } });
    expect(created).toEqual({ path: "Neu/Idee.md", created: true, overwritten: false });
    expect(read("Neu/Idee.md")).toBe("---\ntags:\n  - idee\n---\n# Idee\nText\n");
    expect((await mcp.call("write_note", { path: "Neu/Idee", content: "x" })).isError).toBe(true);

    await mcp.callJson("append_to_note", { path: "Start", content: "- zweiter Eintrag", heading: "Log" });
    expect(read("Start.md")).toMatch(/- erster Eintrag\n- zweiter Eintrag\n$/);

    await mcp.callJson("append_to_note", { path: "Gibts/Noch nicht", content: "Hallo", createIfMissing: true });
    expect(read("Gibts/Noch nicht.md")).toBe("Hallo\n");

    const dry = await mcp.callText("edit_note", { path: "Beta", edits: [{ oldText: "ohne Link", newText: "mit [[Start]]" }], dryRun: true });
    expect(dry).toContain("[dryRun]");
    expect(read("Beta.md")).toContain("ohne Link");
    await mcp.callJson("edit_note", { path: "Beta", edits: [{ oldText: "ohne Link", newText: "mit [[Start]]" }] });
    expect(read("Beta.md")).toContain("mit [[Start]]");
    const bad = await mcp.call("edit_note", { path: "Beta", edits: [{ oldText: "Beta", newText: "X" }] });
    expect(bad.isError).toBe(true);
    expect(bad.content[0]!.text).toMatch(/AMBIGUOUS/);

    await mcp.callJson("replace_section", { path: "Start", heading: "Log", content: "- ersetzt" });
    expect(read("Start.md")).toMatch(/## Log\n- ersetzt\n$/);

    const fm = await mcp.callJson("update_frontmatter", { path: "Beta", set: { status: "archiv", neu: [1, 2] }, addTags: ["archiv"] });
    expect(fm.frontmatter).toEqual({ status: "archiv", prio: 5, tags: ["projekt", "archiv"], neu: [1, 2] });
    expect(read("Beta.md")).toContain("# Beta\n");
  });

  it("Suche: Volltext, Tags, Frontmatter, list_tags", async () => {
    const s = await mcp.callJson("search_notes", { query: "Beta Start" });
    expect(s.results.map((r: any) => r.path)).toEqual(expect.arrayContaining(["Beta.md", "Start.md"]));
    expect(s.results[0].matches[0].line).toBeGreaterThan(0);

    const phrase = await mcp.callJson("search_notes", { query: "alpha-projekt", mode: "phrase" });
    expect(phrase.results.map((r: any) => r.path)).toEqual(["Projekte/Alpha.md"]);
    const rx = await mcp.callJson("search_notes", { query: "^- erster", mode: "regex" });
    expect(rx.total).toBe(1);
    const inFolder = await mcp.callJson("search_notes", { query: "Start", folder: "Projekte" });
    expect(inFolder.results.map((r: any) => r.path)).toEqual(["Projekte/Alpha.md"]);
    expect((await mcp.call("search_notes", { query: "(", mode: "regex" })).isError).toBe(true);

    const tags = await mcp.callJson("search_by_tag", { tags: ["#projekt"] });
    expect(tags.results.map((r: any) => r.path).sort()).toEqual(["Beta.md", "Projekte/Alpha.md"]);
    const exact = await mcp.callJson("search_by_tag", { tags: ["projekt"], includeNested: false });
    expect(exact.results.map((r: any) => r.path)).toEqual(["Beta.md"]);
    const all = await mcp.callJson("search_by_tag", { tags: ["projekt", "idee"], match: "all" });
    expect(all.results.map((r: any) => r.path)).toEqual(["Beta.md"]);

    const st = await mcp.callJson("search_by_frontmatter", { key: "status", operator: "equals", value: "AKTIV" });
    expect(st.results).toEqual([{ path: "Projekte/Alpha.md", value: "aktiv" }]);
    const prio = await mcp.callJson("search_by_frontmatter", { key: "prio", operator: "gte", value: 3 });
    expect(prio.results.map((r: any) => r.path)).toEqual(["Beta.md"]);
    const due = await mcp.callJson("search_by_frontmatter", { key: "due", operator: "lt", value: "2026-12-31" });
    expect(due.results.map((r: any) => r.path)).toEqual(["Projekte/Alpha.md"]);
    const missing = await mcp.callJson("search_by_frontmatter", { key: "status", operator: "missing" });
    expect(missing.total).toBe(3);

    const lt = await mcp.callJson("list_tags");
    expect(lt.tags).toEqual(
      expect.arrayContaining([{ tag: "index", count: 1 }, { tag: "projekt", count: 1 }, { tag: "projekt/aktiv", count: 1 }, { tag: "idee", count: 1 }]),
    );
  });

  it("Graph: Backlinks, ausgehende Links, Orphans, unaufgelöste Links", async () => {
    const bl = await mcp.callJson("get_backlinks", { path: "Start", includeUnlinkedMentions: true });
    expect(bl.backlinks.map((b: any) => b.source)).toEqual(["Projekte/Alpha.md"]);
    expect(bl.backlinks[0].mentions[0].text).toContain("[[Start]]");
    expect(bl.unlinkedMentions.map((u: any) => u.source)).toEqual(["Beta.md"]);

    const betaBl = await mcp.callJson("get_backlinks", { path: "Beta" });
    expect(betaBl.backlinks.map((b: any) => b.source).sort()).toEqual(["Projekte/Alpha.md", "Start.md"]);

    const out = await mcp.callJson("get_outgoing_links", { path: "Notizen/Lose", onlyUnresolved: true });
    expect(out.total).toBe(3);

    const orphans = await mcp.callJson("find_orphans", { includeAttachments: true });
    expect(orphans.orphans.map((o: any) => o.path).sort()).toEqual(["Notizen/Lose.md", "Templates/Daily.md"]);
    expect(orphans.unusedAttachments).toEqual(["Anhänge/unbenutzt.pdf"]);

    const un = await mcp.callJson("find_unresolved_links");
    expect(un.unresolved[0]).toEqual({ target: "Fehlt", count: 2, sources: ["Notizen/Lose.md"] });
  });

  it("move_note aktualisiert alle Links wie Obsidian", async () => {
    const res = await mcp.callJson("move_note", { from: "Beta", to: "Archiv/Beta Alt" });
    expect(res).toMatchObject({ from: "Beta.md", to: "Archiv/Beta Alt.md", moved: true });
    expect(existsSync(path.join(vault.root, "Beta.md"))).toBe(false);
    expect(read("Start.md")).toContain("[[Beta Alt|die Beta]]");
    expect(read("Projekte/Alpha.md")).toContain("[Beta relativ](../Archiv/Beta%20Alt.md)");

    // Notiz mit relativen Links selbst verschieben
    await mcp.callJson("move_note", { from: "Projekte/Alpha.md", to: "Tief/Unten/" });
    const alpha = read("Tief/Unten/Alpha.md");
    expect(alpha).toContain("[Beta relativ](../../Archiv/Beta%20Alt.md)");
    expect(alpha).toContain("![[diagramm.png]]");
    expect(read("Start.md")).toContain("[[Tief/Unten/Alpha]]"); // Link enthielt Pfad → Pfadstil bleibt

    // Anhang verschieben → Embed wird aktualisiert
    await mcp.callJson("move_note", { from: "Anhänge/diagramm.png", to: "Bilder/grafik.png" });
    expect(read("Tief/Unten/Alpha.md")).toContain("![[grafik.png]]");

    const check = await mcp.callJson("find_unresolved_links");
    expect(check.unresolved.map((u: any) => u.target).sort()).toEqual(["Auch weg", "Fehlt"]);
  });

  it("delete_note verschiebt in .trash und warnt vor kaputten Links", async () => {
    const res = await mcp.callJson("delete_note", { path: "Beta" });
    expect(res.trashPath).toBe(".trash/Beta.md");
    expect(res.brokenLinksFrom.sort()).toEqual(["Projekte/Alpha.md", "Start.md"]);
    expect(existsSync(path.join(vault.root, ".trash/Beta.md"))).toBe(true);
    await mcp.callJson("write_note", { path: "Beta", content: "neu" });
    const again = await mcp.callJson("delete_note", { path: "Beta" });
    expect(again.trashPath).toBe(".trash/Beta 1.md");
    const perm = await mcp.callJson("delete_note", { path: "Notizen/Lose", permanent: true });
    expect(perm.trashPath).toBeNull();
    expect(existsSync(path.join(vault.root, "Notizen/Lose.md"))).toBe(false);
  });

  it("Daily Notes mit Obsidian-Einstellungen und Template", async () => {
    const today = new Date();
    const expectedPath = `Journal/${formatDate(today, "YYYY")}/${formatDate(today, "YYYY-MM-DD")}.md`;
    const get = await mcp.callJson("get_daily_note");
    expect(get).toMatchObject({ path: expectedPath, exists: false });

    const res = await mcp.callJson("append_to_daily_note", { content: "- Idee notiert", heading: "Log", timestamp: true });
    expect(res).toMatchObject({ path: expectedPath, created: true });
    const content = read(expectedPath);
    expect(content).toContain(`# ${formatDate(today, "YYYY-MM-DD")}`);
    expect(content).toContain(`Erstellt: ${formatDate(today, "dddd", "de")}`);
    expect(content).toMatch(/## Log\n- \d{2}:\d{2} Idee notiert\n$/);

    await mcp.callJson("append_to_daily_note", { content: "Neuer Abschnitt", heading: "Gedanken" });
    expect(read(expectedPath)).toMatch(/## Gedanken\nNeuer Abschnitt\n$/);

    const y = await mcp.callText("get_daily_note", { date: "gestern", create: true });
    expect(y).toContain('"created":true');
  });
});

describe("Read-only und Multi-Vault", () => {
  it("registriert im Read-only-Modus keine Schreib-Tools und nutzt den richtigen Vault", async () => {
    const a = makeVault({ "A.md": "Vault A" });
    const b = makeVault({ "B.md": "Vault B" });
    const mcp = await connect([
      { name: "eins", path: a.root, readOnly: true },
      { name: "zwei", path: b.root, readOnly: true },
    ]);
    try {
      const { tools } = await mcp.client.listTools();
      const names = tools.map((t) => t.name);
      expect(names).not.toContain("write_note");
      expect(names).not.toContain("delete_note");
      expect(names).toContain("search_notes");
      expect((await mcp.call("read_note", { path: "B.md", vault: "zwei" })).content[0]!.text).toBe("Vault B");
      expect((await mcp.call("read_note", { path: "B" })).isError).toBe(true);
      expect((await mcp.call("read_note", { path: "A", vault: "drei" })).isError).toBe(true);
      const created = await mcp.call("get_daily_note", { create: true });
      expect(created.isError).toBe(true);
      expect(created.content[0]!.text).toMatch(/READ_ONLY/);
    } finally {
      await mcp.close();
      a.cleanup();
      b.cleanup();
    }
  });

  it("gemischt: ein Vault schreibbar, einer nicht", async () => {
    const a = makeVault({ "A.md": "A" });
    const b = makeVault({ "B.md": "B" });
    const mcp = await connect([
      { name: "rw", path: a.root, readOnly: false },
      { name: "ro", path: b.root, readOnly: true },
    ]);
    try {
      expect((await mcp.call("write_note", { path: "X", content: "x", vault: "rw" })).isError).toBeFalsy();
      const denied = await mcp.call("write_note", { path: "X", content: "x", vault: "ro" });
      expect(denied.isError).toBe(true);
      expect(denied.content[0]!.text).toMatch(/schreibgeschützt/);
    } finally {
      await mcp.close();
      a.cleanup();
      b.cleanup();
    }
  });
});
