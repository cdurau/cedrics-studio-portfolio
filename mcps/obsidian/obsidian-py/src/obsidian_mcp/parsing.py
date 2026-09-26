"""Markdown/Obsidian parsing helpers: frontmatter, links, tags, headings, sections."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any
from urllib.parse import unquote

import yaml

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def norm(s: str) -> str:
    """Normalise a name/path for comparisons (NFC + casefold).

    macOS stores file names in NFD, users type NFC ("Ä" vs "A"+"̈").
    """
    return unicodedata.normalize("NFC", s).casefold()


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------

_FM_RE = re.compile(r"\A﻿?---[ \t]*\r?\n(.*?)(?:\r?\n)?^(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)", re.S | re.M)


@dataclass
class Frontmatter:
    data: dict[str, Any]
    raw: str | None  # raw YAML text (None if no frontmatter block)
    end: int  # offset where the body starts
    error: str | None = None


def split_frontmatter(text: str) -> Frontmatter:
    m = _FM_RE.match(text)
    if not m:
        return Frontmatter({}, None, 0)
    raw = m.group(1)
    try:
        data = yaml.safe_load(raw) if raw.strip() else {}
        if data is None:
            data = {}
        if not isinstance(data, dict):
            return Frontmatter({}, raw, m.end(), error="Frontmatter ist kein YAML-Mapping")
        return Frontmatter(data, raw, m.end())
    except yaml.YAMLError as exc:  # keep going, report the problem
        return Frontmatter({}, raw, m.end(), error=f"YAML-Fehler: {exc}")


class _IndentDumper(yaml.SafeDumper):
    """Dump lists Obsidian-style (indented '  - item')."""

    def increase_indent(self, flow: bool = False, indentless: bool = False):  # noqa: ARG002
        return super().increase_indent(flow, False)


def dump_frontmatter(data: dict[str, Any]) -> str:
    if not data:
        return ""
    dumped = yaml.dump(
        data,
        Dumper=_IndentDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=10_000,
    )
    return f"---\n{dumped}---\n"


def join_frontmatter(data: dict[str, Any], body: str) -> str:
    fm = dump_frontmatter(data)
    if not fm:
        return body
    return fm + body


def jsonable(value: Any) -> Any:
    """Convert YAML values (dates etc.) into JSON-friendly values."""
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return value


# ---------------------------------------------------------------------------
# Code masking (links/tags inside code blocks and comments are ignored)
# ---------------------------------------------------------------------------

_FENCE_OPEN = re.compile(r"(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(`+)(?!`)[^\n]+?(?<!`)\1(?!`)")
_COMMENT = re.compile(r"%%.*?%%", re.S)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def mask_code(text: str) -> str:
    """Return a same-length copy of *text* with code/comments blanked out.

    Offsets stay identical, so regex positions map 1:1 onto the original.
    """
    chars = list(text)

    def blank(start: int, end: int) -> None:
        for i in range(start, end):
            if chars[i] != "\n":
                chars[i] = " "

    offset = 0
    fence: str | None = None
    fence_start = 0
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip(" \t")
        indent = len(line) - len(stripped)
        if fence is None:
            m = _FENCE_OPEN.match(stripped)
            if m and indent <= 3:
                fence = m.group(1)
                fence_start = offset
        else:
            m = re.match(r"(`{3,}|~{3,})[ \t]*\r?$", stripped.rstrip("\n"))
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                blank(fence_start, offset + len(line))
                fence = None
        offset += len(line)
    if fence is not None:
        blank(fence_start, len(text))

    for rx in (_INLINE_CODE, _COMMENT, _HTML_COMMENT):
        masked = "".join(chars)
        for m in rx.finditer(masked):
            blank(m.start(), m.end())
    return "".join(chars)


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

_WIKILINK = re.compile(r"(!?)\[\[([^\[\]\n]+?)\]\]")
_MDLINK = re.compile(r"(!?)\[([^\]\n]*)\]\(\s*(<[^>\n]+>|[^)\s]+)(?:\s+[\"'][^\"'\n]*[\"'])?\s*\)")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


@dataclass
class Link:
    kind: str  # "wiki" | "markdown"
    target: str  # note/file part without subpath, e.g. "Folder/Note"
    subpath: str  # "#Heading" / "#^block" or ""
    display: str | None  # alias / link text
    embed: bool
    line: int  # 1-based
    target_span: tuple[int, int]  # offsets of the raw target in the text
    bracketed: bool = False  # markdown link written as <...>

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "subpath": self.subpath or None,
            "display": self.display,
            "embed": self.embed,
            "line": self.line,
        }


def extract_links(text: str, masked: str | None = None) -> list[Link]:
    masked = mask_code(text) if masked is None else masked
    links: list[Link] = []

    # Pre-compute line starts for fast line lookups
    line_starts = [0] + [i + 1 for i, c in enumerate(text) if c == "\n"]

    def line_no(pos: int) -> int:
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    for m in _WIKILINK.finditer(masked):
        inner_start = m.start(2)
        inner = text[m.start(2) : m.end(2)]
        target_part, _, alias = inner.partition("|")
        target_part = target_part.rstrip("\\")  # escaped pipe in tables: [[a\|b]]
        note, hash_, sub = target_part.partition("#")
        links.append(
            Link(
                kind="wiki",
                target=note.strip(),
                subpath=(hash_ + sub).strip() if hash_ else "",
                display=alias.strip() or None,
                embed=bool(m.group(1)),
                line=line_no(m.start()),
                target_span=(inner_start, inner_start + len(note)),
            )
        )

    for m in _MDLINK.finditer(masked):
        raw = text[m.start(3) : m.end(3)]
        bracketed = raw.startswith("<") and raw.endswith(">")
        href = raw[1:-1] if bracketed else raw
        if _SCHEME.match(href) or href.startswith("#"):
            continue
        path, hash_, sub = href.partition("#")
        links.append(
            Link(
                kind="markdown",
                target=unquote(path).strip(),
                subpath=("#" + unquote(sub)) if hash_ else "",
                display=text[m.start(2) : m.end(2)] or None,
                embed=bool(m.group(1)),
                line=line_no(m.start()),
                target_span=(m.start(3), m.end(3)),
                bracketed=bracketed,
            )
        )

    links.sort(key=lambda link: link.target_span[0])
    return links


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

_INLINE_TAG = re.compile(r"(?:^|(?<=[\s(\[,;]))#([\w\-/]+)", re.M)


def _valid_tag(tag: str) -> bool:
    tag = tag.strip("/")
    return bool(tag) and not tag.replace("/", "").isdigit() and any(not c.isdigit() for c in tag)


def frontmatter_tags(data: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    for key in ("tags", "tag", "Tags", "Tag"):
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            items = re.split(r"[,\s]+", value)
        elif isinstance(value, (list, tuple)):
            items = [str(v) for v in value if v is not None]
        else:
            items = [str(value)]
        for item in items:
            item = item.strip().lstrip("#").strip()
            if item and _valid_tag(item):
                tags.append(item)
    return tags


def extract_inline_tags(masked_body: str) -> list[str]:
    tags = []
    for m in _INLINE_TAG.finditer(masked_body):
        tag = m.group(1).rstrip("/")
        if _valid_tag(tag):
            tags.append(tag)
    return tags


# ---------------------------------------------------------------------------
# Headings & sections
# ---------------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$", re.M)


@dataclass
class Heading:
    level: int
    text: str
    line: int
    start: int  # offset of the heading line
    end: int  # offset right after the heading line (incl. newline)


def extract_headings(text: str, masked: str | None = None) -> list[Heading]:
    masked = mask_code(text) if masked is None else masked
    fm = split_frontmatter(text)
    result = []
    for m in _HEADING.finditer(masked, fm.end):
        end = m.end()
        if end < len(text) and text[end] == "\n":
            end += 1
        result.append(
            Heading(
                level=len(m.group(1)),
                text=text[m.start(2) : m.end(2)].strip(),
                line=text.count("\n", 0, m.start()) + 1,
                start=m.start(),
                end=end,
            )
        )
    return result


def find_section(text: str, heading: str) -> tuple[Heading, int] | None:
    """Return (heading, section_end_offset) for the first heading matching *heading*."""
    wanted = norm(heading.strip().lstrip("#").strip())
    headings = extract_headings(text)
    for i, h in enumerate(headings):
        if norm(h.text) == wanted:
            end = len(text)
            for nxt in headings[i + 1 :]:
                if nxt.level <= h.level:
                    end = nxt.start
                    break
            return h, end
    return None


# ---------------------------------------------------------------------------
# Parsed note
# ---------------------------------------------------------------------------


@dataclass
class NoteInfo:
    text: str
    frontmatter: dict[str, Any]
    frontmatter_error: str | None
    body_start: int
    links: list[Link] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)  # unique, original casing

    @property
    def body(self) -> str:
        return self.text[self.body_start :]


def parse_note(text: str) -> NoteInfo:
    fm = split_frontmatter(text)
    masked = mask_code(text)
    links = extract_links(text, masked)
    tags_all = frontmatter_tags(fm.data) + extract_inline_tags(masked[fm.end :])
    seen: set[str] = set()
    tags: list[str] = []
    for t in tags_all:
        key = norm(t)
        if key not in seen:
            seen.add(key)
            tags.append(t)
    return NoteInfo(
        text=text,
        frontmatter=fm.data,
        frontmatter_error=fm.error,
        body_start=fm.end,
        links=links,
        tags=tags,
    )
