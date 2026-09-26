import json
import unicodedata
from datetime import date, datetime

import pytest

from obsidian_mcp import parsing
from obsidian_mcp.config import VaultConfig, parse_vault_spec
from obsidian_mcp.datefmt import format_moment, parse_date_arg, render_template
from obsidian_mcp.vault import Vault, VaultError


def make_vault(tmp_path, files: dict[str, str], read_only=False, obsidian: dict | None = None) -> Vault:
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    (tmp_path / ".obsidian").mkdir(exist_ok=True)
    for name, data in (obsidian or {}).items():
        (tmp_path / ".obsidian" / name).write_text(json.dumps(data), encoding="utf-8")
    return Vault(VaultConfig(name="test", path=tmp_path, read_only=read_only))


FILES = {
    "Index.md": "---\ntags: [hub]\nstatus: aktiv\n---\n# Index\nSiehe [[Projekte/Alpha]] und [[Beta|B]].\n"
                "Bild: ![[bild.png]]\n`[[NichtImCode]]`\n```\n[[AuchNicht]] #keintag\n```\n#inline/tag text\n",
    "Projekte/Alpha.md": "# Alpha\nLink zurück zu [[Index#Index]].\n[Beta md](../Beta.md)\n\n## Aufgaben\n- a\n\n## Notizen\nx\n",
    "Beta.md": "---\ntags:\n  - projekt/beta\n---\nBeta Inhalt mit Suchwort Kaffee.\n[[Fehlt]]\n",
    "Waise.md": "Niemand verlinkt mich. Kaffee Kaffee.\n",
    "bild.png": "PNG",
    ".obsidian/app.json": "{}",
}


def test_parsing_links_tags_and_code_masking():
    ni = parsing.parse_note(FILES["Index.md"])
    targets = [l.target for l in ni.links]
    assert targets == ["Projekte/Alpha", "Beta", "bild.png"]
    assert ni.links[1].display == "B"
    assert ni.links[2].embed
    assert set(ni.tags) == {"hub", "inline/tag"}
    assert ni.frontmatter["status"] == "aktiv"


def test_headings_ignore_code_and_frontmatter():
    text = "---\na: 1\n---\n# Echt\n```\n# Fake\n```\n## Zwei\n"
    assert [h.text for h in parsing.extract_headings(text)] == ["Echt", "Zwei"]


def test_tags_not_numbers_or_urls():
    ni = parsing.parse_note("Issue #123 und https://x.de/#anker und #ok-tag und (#klammer)")
    assert ni.tags == ["ok-tag", "klammer"]


def test_list_and_read(tmp_path):
    v = make_vault(tmp_path, FILES)
    res = v.list_notes()
    assert set(res["files"]) == {"Index.md", "Projekte/Alpha.md", "Beta.md", "Waise.md"}
    assert res["subfolders"] == ["Projekte"]
    note = v.read_note("Alpha")  # resolved by unique name
    assert note["path"] == "Projekte/Alpha.md"
    assert [h["text"] for h in note["headings"]] == ["Alpha", "Aufgaben", "Notizen"]
    sec = v.read_note("Projekte/Alpha", section="Aufgaben")
    assert sec["content"].strip() == "## Aufgaben\n- a"


def test_path_traversal_blocked(tmp_path):
    v = make_vault(tmp_path, FILES)
    for bad in ["../etc/passwd", ".obsidian/app.json", "Projekte/../../x"]:
        with pytest.raises(VaultError):
            v.read_note(bad)


def test_search(tmp_path):
    v = make_vault(tmp_path, FILES)
    res = v.search("kaffee")
    assert [r["path"] for r in res["results"]] == ["Waise.md", "Beta.md"]
    assert v.search("kaffee beta")["total"] == 1
    assert v.search(r"Such\w+", regex=True)["results"][0]["path"] == "Beta.md"


def test_frontmatter_search(tmp_path):
    v = make_vault(tmp_path, FILES)
    assert [r["path"] for r in v.search_frontmatter("status", "aktiv")] == ["Index.md"]
    assert [r["path"] for r in v.search_frontmatter("tags", "projekt")] == ["Beta.md"]


def test_links_backlinks_orphans_unresolved(tmp_path):
    v = make_vault(tmp_path, FILES)
    out = {l["target"]: l["resolved"] for l in v.outgoing_links("Index")}
    assert out == {"Projekte/Alpha": "Projekte/Alpha.md", "Beta": "Beta.md", "bild.png": "bild.png"}
    back = {b["source"] for b in v.backlinks("Beta")}
    assert back == {"Index.md", "Projekte/Alpha.md"}  # wikilink + relative markdown link
    assert v.orphans() == ["Waise.md"]
    assert [u["target"] for u in v.unresolved()] == ["Fehlt"]


def test_tags(tmp_path):
    v = make_vault(tmp_path, FILES)
    tags = {t["tag"]: t["count"] for t in v.tags()}
    assert tags == {"#hub": 1, "#inline/tag": 1, "#projekt/beta": 1}
    assert v.notes_by_tag("#projekt") == ["Beta.md"]
    assert v.notes_by_tag("projekt", include_nested=False) == []


def test_create_append_replace(tmp_path):
    v = make_vault(tmp_path, FILES)
    v.create_note("Neu/Idee", "Text", frontmatter={"tags": ["idee"]})
    txt = (tmp_path / "Neu/Idee.md").read_text()
    assert txt.startswith("---\ntags:\n  - idee\n---\nText")
    with pytest.raises(VaultError):
        v.create_note("Neu/Idee", "x")
    v.append("Projekte/Alpha", "- b", heading="Aufgaben")
    alpha = (tmp_path / "Projekte/Alpha.md").read_text()
    assert "## Aufgaben\n- a\n- b\n\n## Notizen" in alpha
    v.append("Projekte/Alpha", "Neu", heading="Log")
    assert (tmp_path / "Projekte/Alpha.md").read_text().endswith("## Log\nNeu\n")
    v.append("Beta", "Oben", prepend=True)
    assert "---\nOben\nBeta Inhalt" in (tmp_path / "Beta.md").read_text()
    assert v.replace_in_note("Beta", "Kaffee", "Tee")["replacements"] == 1
    with pytest.raises(VaultError):
        v.replace_in_note("Beta", "gibtsnicht", "x")


def test_replace_in_note_dry_run(tmp_path):
    v = make_vault(tmp_path, FILES)
    before = (tmp_path / "Waise.md").read_text()
    res = v.replace_in_note("Waise", "Kaffee", "Tee", dry_run=True)
    assert res["dry_run"] and res["changed"] and res["replacements"] == 2
    assert "-Niemand verlinkt mich. Kaffee Kaffee." in res["diff"]
    assert "+Niemand verlinkt mich. Tee Tee." in res["diff"]
    assert (tmp_path / "Waise.md").read_text() == before


def test_replace_section(tmp_path):
    v = make_vault(tmp_path, FILES)
    # middle section: next heading stays separated by a blank line
    res = v.replace_section("Projekte/Alpha", "Aufgaben", "- neu 1\n- neu 2\n\n")
    assert res == {"path": "Projekte/Alpha.md", "heading": "Aufgaben", "changed": True}
    alpha = (tmp_path / "Projekte/Alpha.md").read_text()
    assert alpha.endswith("## Aufgaben\n- neu 1\n- neu 2\n\n## Notizen\nx\n")
    # last section
    v.replace_section("Projekte/Alpha", "## Notizen", "y")
    assert (tmp_path / "Projekte/Alpha.md").read_text().endswith("## Notizen\ny\n")
    # empty body, subsections belong to the section and are replaced too
    (tmp_path / "S.md").write_text("# A\ntext\n## A1\nsub\n# B\nb\n")
    v.replace_section("S", "A", "")
    assert (tmp_path / "S.md").read_text() == "# A\n\n# B\nb\n"
    # heading at end of file without newline
    (tmp_path / "T.md").write_text("# T")
    v.replace_section("T", "T", "neu")
    assert (tmp_path / "T.md").read_text() == "# T\nneu\n"
    # dry run writes nothing
    before = (tmp_path / "Projekte/Alpha.md").read_text()
    res = v.replace_section("Projekte/Alpha", "Aufgaben", "anders", dry_run=True)
    assert res["dry_run"] and "+anders" in res["diff"]
    assert (tmp_path / "Projekte/Alpha.md").read_text() == before
    with pytest.raises(VaultError, match="nicht gefunden"):
        v.replace_section("Projekte/Alpha", "Gibtsnicht", "x")
    ro = make_vault(tmp_path, {}, read_only=True)
    with pytest.raises(VaultError):
        ro.replace_section("Projekte/Alpha", "Aufgaben", "x")
    assert ro.replace_section("Projekte/Alpha", "Aufgaben", "x", dry_run=True)["dry_run"]


def test_update_frontmatter(tmp_path):
    v = make_vault(tmp_path, FILES)
    res = v.update_frontmatter("Waise", {"status": "erledigt"}, add_tags=["#neu"])
    assert res["frontmatter"] == {"status": "erledigt", "tags": ["neu"]}
    text = (tmp_path / "Waise.md").read_text()
    assert text.endswith("Niemand verlinkt mich. Kaffee Kaffee.\n")
    v.update_frontmatter("Waise", remove_keys=["status"], remove_tags=["neu"])
    assert (tmp_path / "Waise.md").read_text() == "Niemand verlinkt mich. Kaffee Kaffee.\n"


def test_move_updates_links(tmp_path):
    v = make_vault(tmp_path, FILES)
    res = v.move_note("Beta", "Archiv/Beta 2")
    assert res["to"] == "Archiv/Beta 2.md"
    assert set(res["links_updated_in"]) == {"Index.md", "Projekte/Alpha.md"}
    idx = (tmp_path / "Index.md").read_text()
    assert "[[Beta 2|B]]" in idx
    alpha = (tmp_path / "Projekte/Alpha.md").read_text()
    assert "[Beta md](../Archiv/Beta%202.md)" in alpha
    assert {b["source"] for b in v.backlinks("Archiv/Beta 2")} == {"Index.md", "Projekte/Alpha.md"}
    # moving a note fixes its own relative markdown links
    v.move_note("Projekte/Alpha", "Alpha")
    assert "[Beta md](Archiv/Beta%202.md)" in (tmp_path / "Alpha.md").read_text()
    assert v.unresolved()[0]["target"] == "Fehlt"
    assert len(v.unresolved()) == 1


def test_delete_to_trash(tmp_path):
    v = make_vault(tmp_path, FILES)
    res = v.delete_note("Waise")
    assert res["trash_file"] == ".trash/Waise.md"
    assert (tmp_path / ".trash/Waise.md").exists()
    assert "Waise.md" not in v.list_notes()["files"]


def test_read_only(tmp_path):
    v = make_vault(tmp_path, FILES, read_only=True)
    with pytest.raises(VaultError):
        v.create_note("x", "y")
    with pytest.raises(VaultError):
        v.delete_note("Waise")


def test_daily_note_with_template(tmp_path):
    files = dict(FILES)
    files["Vorlagen/Tag.md"] = "# {{title}}\nErstellt {{date:dddd, D. MMMM YYYY}} um {{time}}\n## Log\n"
    v = make_vault(tmp_path, files, obsidian={
        "daily-notes.json": {"folder": "Journal", "format": "YYYY/MM/YYYY-MM-DD", "template": "Vorlagen/Tag"},
        "templates.json": {"folder": "Vorlagen"},
    })
    res = v.daily_note("2026-09-26")
    assert res["path"] == "Journal/2026/09/2026-09-26.md" and res["created"]
    assert "Erstellt Samstag, 26. September 2026" in res["content"]
    assert res["content"].startswith("# 2026-09-26\n")
    v.append(v.daily_note_path("2026-09-26"), "- Kaffee getrunken", heading="Log")
    assert v.daily_note("2026-09-26")["content"].endswith("## Log\n- Kaffee getrunken\n")
    assert v.list_templates()["templates"] == ["Vorlagen/Tag.md"]
    made = v.create_from_template("Tag", "Meeting", variables={"extra": "x"})
    assert made["template"] == "Vorlagen/Tag.md"
    assert (tmp_path / "Meeting.md").read_text().startswith("# Meeting\n")


def test_unicode_nfd_names(tmp_path):
    nfd = unicodedata.normalize("NFD", "Ärger.md")
    v = make_vault(tmp_path, {nfd: "inhalt", "Link.md": "[[Ärger]]"})
    assert v.read_note("Ärger")["content"] == "inhalt"
    assert v.backlinks("Ärger")[0]["source"] == "Link.md"
    v.append("Ärger", "mehr")
    assert v.read_note("Ärger")["content"] == "inhalt\nmehr\n"


def test_moment_format_and_dates():
    d = datetime(2026, 1, 5, 14, 7)
    assert format_moment("YYYY-MM-DD", d) == "2026-01-05"
    assert format_moment("dddd, Do MMMM [KW]W", d, "de") == "Montag, 5. Januar KW2"
    assert format_moment("ddd MMM Do, h:mm A", d, "en") == "Mon Jan 5th, 2:07 PM"
    assert parse_date_arg("gestern", date(2026, 3, 1)) == date(2026, 2, 28)
    assert parse_date_arg("+2", date(2026, 3, 1)) == date(2026, 3, 3)
    assert render_template("{{title}} {{date}} {{foo}} {{bar}}", title="T", now=d,
                           variables={"foo": "F"}) == "T 2026-01-05 F {{bar}}"


def test_vault_spec():
    vc = parse_vault_spec("Privat=/tmp:ro")
    assert vc.name == "privat" and vc.read_only
    assert parse_vault_spec("/tmp").name == "tmp"
