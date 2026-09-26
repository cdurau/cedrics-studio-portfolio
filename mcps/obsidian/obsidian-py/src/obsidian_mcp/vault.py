"""Filesystem access to one Obsidian vault: index, link resolution, reads and writes."""

from __future__ import annotations

import difflib
import json
import os
import posixpath
import re
import shlex
import tempfile
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import parsing
from .config import VaultConfig
from .datefmt import format_moment, parse_date_arg, render_template
from .parsing import NoteInfo, jsonable, norm

NOTE_EXT = ".md"


class VaultError(ValueError):
    """User-facing error (becomes an MCP tool error)."""


class NoteNotFound(VaultError):
    pass


def _strip_md(p: str) -> str:
    return p[: -len(NOTE_EXT)] if p.lower().endswith(NOTE_EXT) else p


@dataclass
class _Index:
    notes: list[str] = field(default_factory=list)  # rel posix paths of .md files
    files: list[str] = field(default_factory=list)  # all files (incl. attachments)
    by_path: dict[str, str] = field(default_factory=dict)  # norm(rel) / norm(rel w/o .md) -> rel
    by_name: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    folders: list[str] = field(default_factory=list)


class Vault:
    def __init__(self, cfg: VaultConfig):
        self.cfg = cfg
        self.name = cfg.name
        self.root = cfg.path.resolve()
        self.read_only = cfg.read_only
        self._cache: dict[str, tuple[int, int, NoteInfo]] = {}
        self._realnames: dict[str, str] = {}  # NFC rel path -> on-disk rel path (if different, e.g. NFD)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ paths

    def _excluded(self, part: str) -> bool:
        return part.startswith(".") or part in self.cfg.exclude

    def _abs(self, rel: str) -> Path:
        real = self._realnames.get(rel)
        if real is None and "/" in rel:
            parent, _, leaf = rel.rpartition("/")
            real_parent = self._realnames.get(parent)
            if real_parent is not None:
                real = f"{real_parent}/{leaf}"
        return self.root / (real or rel)

    def _check_inside(self, rel: str) -> str:
        rel = parsing.nfc(rel.strip().replace("\\", "/")).lstrip("/")
        rel = posixpath.normpath(rel) if rel else ""
        if rel in ("", "."):
            return ""
        if rel.startswith("../") or rel == "..":
            raise VaultError("Pfad liegt außerhalb des Vaults.")
        if any(self._excluded(p) for p in rel.split("/")):
            raise VaultError(f"Zugriff auf versteckte/ausgeschlossene Ordner ist nicht erlaubt: {rel}")
        full = os.path.realpath(self._abs(rel))
        if os.path.commonpath([full, str(self.root)]) != str(self.root):
            raise VaultError("Pfad liegt außerhalb des Vaults.")
        return rel

    def _ensure_writable(self) -> None:
        if self.read_only:
            raise VaultError(f"Vault '{self.name}' ist schreibgeschützt (read_only).")

    def note_path(self, path: str, *, must_exist: bool = True) -> str:
        """Turn user input ("Folder/Note", "Note.md", "note") into a vault-relative .md path."""
        if not path or not path.strip():
            raise VaultError("Kein Pfad angegeben.")
        rel = path.strip()
        if not rel.lower().endswith(NOTE_EXT):
            rel += NOTE_EXT
        rel = self._check_inside(rel)
        if self._abs(rel).is_file():
            return rel
        idx = self.index()
        hit = idx.by_path.get(norm(rel))  # different case / Unicode normalisation (macOS NFD)
        if hit and hit.lower().endswith(NOTE_EXT):
            return hit
        # Obsidian-style name resolution ("Einkaufsliste" -> "Listen/Einkaufsliste.md"),
        # only for existing notes – new paths are always taken literally.
        if must_exist and "/" not in path.strip().strip("/"):
            cands = [p for p in idx.by_name.get(norm(_strip_md(path.strip())), []) if p.lower().endswith(NOTE_EXT)]
            if len(cands) == 1:
                return cands[0]
            if len(cands) > 1:
                raise VaultError(f"Mehrdeutiger Notizname '{path}'. Kandidaten: {', '.join(cands)}")
        if must_exist:
            raise NoteNotFound(f"Notiz nicht gefunden: {path}")
        return rel

    def existing_or_new(self, path: str) -> str:
        """Existing note (with name resolution) or the literal new path."""
        try:
            return self.note_path(path)
        except NoteNotFound:
            return self.note_path(path, must_exist=False)

    def file_path(self, path: str) -> str:
        """Existing file of any type (attachments), resolved like an Obsidian link."""
        rel = self._check_inside(path)
        if self._abs(rel).is_file():
            return rel
        idx = self.index()
        hit = idx.by_path.get(norm(rel)) or self.resolve_link(path, "", idx)
        if hit:
            return hit
        raise NoteNotFound(f"Datei nicht gefunden: {path}")

    def folder_path(self, folder: str | None) -> str:
        if not folder or folder.strip() in ("", "/", "."):
            return ""
        rel = self._check_inside(folder.strip().strip("/"))
        if not self._abs(rel).is_dir():
            idx = self.index()
            for f in idx.folders:
                if norm(f) == norm(rel):
                    return f
            raise VaultError(f"Ordner nicht gefunden: {folder}")
        return rel

    # ------------------------------------------------------------------ index

    def index(self) -> _Index:
        idx = _Index()
        root = str(self.root)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not self._excluded(d))
            rel_dir = os.path.relpath(dirpath, root)
            rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
            if rel_dir:
                idx.folders.append(parsing.nfc(rel_dir))
                if parsing.nfc(rel_dir) != rel_dir:
                    self._realnames[parsing.nfc(rel_dir)] = rel_dir
            for fn in sorted(filenames):
                if fn.startswith("."):
                    continue
                disk_rel = f"{rel_dir}/{fn}" if rel_dir else fn
                rel = parsing.nfc(disk_rel)
                if rel != disk_rel:
                    self._realnames[rel] = disk_rel
                idx.files.append(rel)
                idx.by_path[norm(rel)] = rel
                base = rel.rsplit("/", 1)[-1]
                idx.by_name[norm(base)].append(rel)
                if fn.lower().endswith(NOTE_EXT):
                    idx.notes.append(rel)
                    idx.by_path.setdefault(norm(_strip_md(rel)), rel)
                    idx.by_name[norm(_strip_md(base))].append(rel)
        return idx

    def info(self, rel: str) -> NoteInfo:
        """Parsed note with an mtime-based cache."""
        full = self._abs(rel)
        st = full.stat()
        with self._lock:
            cached = self._cache.get(rel)
            if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
                return cached[2]
        text = full.read_text(encoding="utf-8", errors="replace")
        ni = parsing.parse_note(text)
        with self._lock:
            self._cache[rel] = (st.st_mtime_ns, st.st_size, ni)
        return ni

    def _iter_infos(self, idx: _Index, folder: str = ""):
        prefix = f"{folder}/" if folder else ""
        for rel in idx.notes:
            if prefix and not rel.startswith(prefix):
                continue
            try:
                yield rel, self.info(rel)
            except OSError:
                continue

    # ------------------------------------------------------------------ link resolution

    def resolve_link(self, link: parsing.Link | str, source: str, idx: _Index | None = None) -> str | None:
        idx = idx or self.index()
        if isinstance(link, str):
            target, kind = link, "wiki"
        else:
            target, kind = link.target, link.kind
        if not target:
            return source  # [[#Heading]] -> self
        t = parsing.nfc(target.strip().replace("\\", "/"))
        if kind == "markdown" or t.startswith(("./", "../")):
            cand = posixpath.normpath(posixpath.join(posixpath.dirname(source), t))
            hit = idx.by_path.get(norm(cand))
            if hit:
                return hit
        t = t.lstrip("/")
        hit = idx.by_path.get(norm(t))
        if hit:
            return hit
        if "/" in t:
            suffix = "/" + norm(_strip_md(t))
            cands = [p for p in idx.files if ("/" + norm(_strip_md(p))).endswith(suffix)]
        else:
            cands = list(idx.by_name.get(norm(t), []))
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]
        src_dir = posixpath.dirname(source)
        same = [c for c in cands if posixpath.dirname(c) == src_dir]
        if same:
            return same[0]
        notes_first = sorted(cands, key=lambda c: (not c.lower().endswith(NOTE_EXT), c.count("/"), len(c), c))
        return notes_first[0]

    # ------------------------------------------------------------------ read API

    def summary(self) -> dict[str, Any]:
        idx = self.index()
        return {
            "name": self.name,
            "path": str(self.root),
            "read_only": self.read_only,
            "notes": len(idx.notes),
            "attachments": len(idx.files) - len(idx.notes),
            "folders": len(idx.folders),
            "daily_notes": self.daily_settings(),
            "templates_folder": self.templates_settings()["folder"] or None,
        }

    def list_notes(self, folder: str | None = None, recursive: bool = True,
                   include_attachments: bool = False, limit: int = 500) -> dict[str, Any]:
        folder_rel = self.folder_path(folder)
        idx = self.index()
        prefix = f"{folder_rel}/" if folder_rel else ""
        source = idx.files if include_attachments else idx.notes
        items = []
        for rel in source:
            if prefix and not rel.startswith(prefix):
                continue
            if not recursive and "/" in rel[len(prefix):]:
                continue
            items.append(rel)
        subfolders = [f for f in idx.folders if f.startswith(prefix) and "/" not in f[len(prefix):]]
        return {
            "folder": folder_rel or "/",
            "total": len(items),
            "truncated": len(items) > limit,
            "files": items[:limit],
            "subfolders": subfolders,
        }

    def list_folders(self) -> list[str]:
        return self.index().folders

    def read_note(self, path: str, section: str | None = None, include_metadata: bool = True) -> dict[str, Any]:
        rel = self.note_path(path)
        ni = self.info(rel)
        st = self._abs(rel).stat()
        result: dict[str, Any] = {"path": rel}
        if section:
            found = parsing.find_section(ni.text, section)
            if not found:
                heads = [h.text for h in parsing.extract_headings(ni.text)]
                raise VaultError(f"Überschrift '{section}' nicht gefunden. Vorhanden: {heads}")
            h, end = found
            result["section"] = h.text
            result["content"] = ni.text[h.start:end].rstrip() + "\n"
        else:
            result["content"] = ni.text
        if include_metadata:
            result.update(
                frontmatter=jsonable(ni.frontmatter),
                frontmatter_error=ni.frontmatter_error,
                tags=ni.tags,
                headings=[{"level": h.level, "text": h.text, "line": h.line}
                          for h in parsing.extract_headings(ni.text)],
                outgoing_links=len(ni.links),
                size=st.st_size,
                modified=datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            )
        return result

    def read_many(self, paths: list[str], max_chars_each: int = 20_000) -> list[dict[str, Any]]:
        out = []
        for p in paths:
            try:
                rel = self.note_path(p)
                text = self.info(rel).text
                out.append({"path": rel, "content": text[:max_chars_each],
                            "truncated": len(text) > max_chars_each})
            except (VaultError, OSError) as exc:
                out.append({"path": p, "error": str(exc)})
        return out

    def recent(self, limit: int = 20, folder: str | None = None) -> list[dict[str, Any]]:
        folder_rel = self.folder_path(folder)
        prefix = f"{folder_rel}/" if folder_rel else ""
        items = []
        for rel in self.index().notes:
            if prefix and not rel.startswith(prefix):
                continue
            try:
                items.append((self._abs(rel).stat().st_mtime, rel))
            except OSError:
                pass
        items.sort(reverse=True)
        return [{"path": rel, "modified": datetime.fromtimestamp(m).isoformat(timespec="seconds")}
                for m, rel in items[:limit]]

    def search(self, query: str, *, regex: bool = False, case_sensitive: bool = False,
               folder: str | None = None, limit: int = 20, snippets_per_note: int = 3) -> dict[str, Any]:
        if not query or not query.strip():
            raise VaultError("Leere Suchanfrage.")
        flags = 0 if case_sensitive else re.IGNORECASE
        if regex:
            try:
                patterns = [re.compile(query, flags | re.M)]
            except re.error as exc:
                raise VaultError(f"Ungültiger Regex: {exc}") from exc
        else:
            try:
                terms = shlex.split(query)
            except ValueError:
                terms = query.split()
            patterns = [re.compile(re.escape(parsing.nfc(t)), flags) for t in terms if t.strip()]
        folder_rel = self.folder_path(folder)
        results = []
        for rel, ni in self._iter_infos(self.index(), folder_rel):
            text = parsing.nfc(ni.text)
            name = rel.rsplit("/", 1)[-1]
            score = 0
            ok = True
            for pat in patterns:
                n_text = len(pat.findall(text))
                n_name = 1 if pat.search(parsing.nfc(rel)) else 0
                if n_text + n_name == 0:
                    ok = False
                    break
                score += n_text + 10 * n_name
            if not ok:
                continue
            snippets = []
            for i, line in enumerate(text.splitlines(), 1):
                m = next((mm for p in patterns if (mm := p.search(line))), None)
                if m:
                    start = max(0, m.start() - 80)
                    snip = line[start:start + 220].strip()
                    snippets.append({"line": i, "text": ("…" if start else "") + snip})
                    if len(snippets) >= snippets_per_note:
                        break
            results.append({"path": rel, "title": _strip_md(name), "score": score, "matches": snippets})
        results.sort(key=lambda r: (-r["score"], r["path"]))
        return {"query": query, "total": len(results), "results": results[:limit]}

    def search_frontmatter(self, key: str, value: str | None = None, folder: str | None = None,
                           limit: int = 100) -> list[dict[str, Any]]:
        wanted_key = norm(key)
        wanted_val = norm(str(value)) if value is not None else None
        out = []
        for rel, ni in self._iter_infos(self.index(), self.folder_path(folder)):
            for k, v in ni.frontmatter.items():
                if norm(str(k)) != wanted_key:
                    continue
                if wanted_val is None:
                    hit = True
                else:
                    vals = v if isinstance(v, (list, tuple)) else [v]
                    hit = any(wanted_val == norm(str(jsonable(x))) or wanted_val in norm(str(jsonable(x)))
                              for x in vals)
                if hit:
                    out.append({"path": rel, "value": jsonable(v)})
                break
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------ links & tags

    def outgoing_links(self, path: str) -> list[dict[str, Any]]:
        rel = self.note_path(path)
        idx = self.index()
        out = []
        for link in self.info(rel).links:
            d = link.as_dict()
            d["resolved"] = self.resolve_link(link, rel, idx)
            out.append(d)
        return out

    def backlinks(self, path: str) -> list[dict[str, Any]]:
        rel = self.note_path(path)
        idx = self.index()
        out = []
        for src, ni in self._iter_infos(idx):
            lines = None
            for link in ni.links:
                if src == rel and not link.target:
                    continue  # self-reference to own heading
                if self.resolve_link(link, src, idx) == rel:
                    lines = lines or ni.text.splitlines()
                    ctx = lines[link.line - 1].strip() if link.line - 1 < len(lines) else ""
                    out.append({"source": src, "line": link.line, "context": ctx[:240],
                                "subpath": link.subpath or None, "embed": link.embed})
        return out

    def tags(self, prefix: str | None = None) -> list[dict[str, Any]]:
        counts: dict[str, int] = defaultdict(int)
        display: dict[str, str] = {}
        for _, ni in self._iter_infos(self.index()):
            for t in ni.tags:
                key = norm(t)
                counts[key] += 1
                display.setdefault(key, t)
        p = norm(prefix.lstrip("#")) if prefix else None
        items = [{"tag": "#" + display[k], "count": c} for k, c in counts.items() if not p or k.startswith(p)]
        items.sort(key=lambda d: (-d["count"], d["tag"]))
        return items

    def notes_by_tag(self, tag: str, include_nested: bool = True) -> list[str]:
        wanted = norm(tag.strip().lstrip("#"))
        out = []
        for rel, ni in self._iter_infos(self.index()):
            for t in ni.tags:
                k = norm(t)
                if k == wanted or (include_nested and k.startswith(wanted + "/")):
                    out.append(rel)
                    break
        return out

    def _link_graph(self) -> tuple[_Index, dict[str, set[str]], list[dict[str, Any]]]:
        idx = self.index()
        incoming: dict[str, set[str]] = defaultdict(set)
        unresolved = []
        for src, ni in self._iter_infos(idx):
            for link in ni.links:
                tgt = self.resolve_link(link, src, idx)
                if tgt is None:
                    unresolved.append({"source": src, "target": link.target, "line": link.line})
                elif tgt != src:
                    incoming[tgt].add(src)
        return idx, incoming, unresolved

    def orphans(self, strict: bool = False) -> list[str]:
        idx, incoming, _ = self._link_graph()
        result = []
        for rel in idx.notes:
            if incoming.get(rel):
                continue
            if strict:
                try:
                    if any(link.target for link in self.info(rel).links):
                        continue
                except OSError:
                    continue
            result.append(rel)
        return result

    def unresolved(self) -> list[dict[str, Any]]:
        _, _, unresolved = self._link_graph()
        grouped: dict[str, list[str]] = defaultdict(list)
        for u in unresolved:
            grouped[u["target"]].append(f'{u["source"]}:{u["line"]}')
        return [{"target": t, "count": len(s), "sources": s[:20]}
                for t, s in sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0]))]

    # ------------------------------------------------------------------ write helpers

    def _write(self, rel: str, text: str) -> None:
        full = self._abs(rel)
        full.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=full.parent, prefix=".obsidian-mcp-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
            if full.exists():
                try:
                    os.chmod(tmp, full.stat().st_mode & 0o777)
                except OSError:
                    pass
            os.replace(tmp, full)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        with self._lock:
            self._cache.pop(rel, None)

    # ------------------------------------------------------------------ write API

    def create_note(self, path: str, content: str = "", frontmatter: dict[str, Any] | None = None,
                    overwrite: bool = False) -> dict[str, Any]:
        self._ensure_writable()
        rel = self.note_path(path, must_exist=False)
        existed = self._abs(rel).exists()
        if existed and not overwrite:
            raise VaultError(f"Notiz existiert bereits: {rel} (overwrite=true zum Überschreiben)")
        text = content
        if frontmatter:
            fm = parsing.split_frontmatter(content)
            merged = {**fm.data, **frontmatter}
            text = parsing.join_frontmatter(merged, content[fm.end:])
        if text and not text.endswith("\n"):
            text += "\n"
        self._write(rel, text)
        return {"path": rel, "created": not existed, "overwritten": existed}

    def append(self, path: str, content: str, heading: str | None = None,
               create_if_missing: bool = True, prepend: bool = False) -> dict[str, Any]:
        self._ensure_writable()
        rel = self.existing_or_new(path)
        full = self._abs(rel)
        created = False
        if not full.exists():
            if not create_if_missing:
                raise VaultError(f"Notiz nicht gefunden: {path}")
            text, created = "", True
        else:
            text = full.read_text(encoding="utf-8")
        block = content.rstrip("\n") + "\n"
        info: dict[str, Any] = {"path": rel, "created": created}
        if heading:
            found = parsing.find_section(text, heading)
            if found:
                h, end = found
                if prepend:
                    pos = h.end
                    text = text[:pos] + block + text[pos:]
                else:
                    section = text[h.end:end]
                    trimmed = section.rstrip("\n")
                    pos = h.end + len(trimmed)
                    before = text[:pos]
                    sep = "\n" if trimmed else ""
                    after = text[end:]
                    text = before + sep + block + ("\n" if after else "") + after.lstrip("\n")
                info["heading"] = h.text
            else:
                title = heading.strip()
                if not title.startswith("#"):
                    title = "## " + title
                if text and not text.endswith("\n"):
                    text += "\n"
                text += ("\n" if text.strip() else "") + title + "\n" + block
                info["heading_created"] = title
        elif prepend:
            fm = parsing.split_frontmatter(text)
            text = text[:fm.end] + block + text[fm.end:]
        else:
            if text and not text.endswith("\n"):
                text += "\n"
            text += block
        self._write(rel, text)
        return info

    def replace_in_note(self, path: str, old: str, new: str, regex: bool = False, count: int = 0,
                        dry_run: bool = False) -> dict[str, Any]:
        if not dry_run:
            self._ensure_writable()
        rel = self.note_path(path)
        text = self._abs(rel).read_text(encoding="utf-8")
        if regex:
            try:
                new_text, n = re.subn(old, new, text, count=count, flags=re.M)
            except re.error as exc:
                raise VaultError(f"Ungültiger Regex: {exc}") from exc
        else:
            n = text.count(old)
            new_text = text.replace(old, new, count if count > 0 else -1)
            n = min(n, count) if count > 0 else n
        if n == 0:
            raise VaultError("Suchtext nicht gefunden – nichts geändert.")
        return self._commit(rel, text, new_text, dry_run, {"path": rel, "replacements": n})

    def replace_section(self, path: str, heading: str, content: str, dry_run: bool = False) -> dict[str, Any]:
        """Replace everything below *heading* up to the next heading of the same or higher level.
        The heading line itself is kept."""
        if not dry_run:
            self._ensure_writable()
        rel = self.note_path(path)
        text = self._abs(rel).read_text(encoding="utf-8")
        found = parsing.find_section(text, heading)
        if not found:
            heads = [h.text for h in parsing.extract_headings(text)]
            raise VaultError(f"Überschrift '{heading}' nicht gefunden. Vorhanden: {heads}")
        h, end = found
        is_last = end >= len(text)
        body = content.strip("\n")
        body = body + "\n" if body else ""
        if not is_last:
            body += "\n"  # blank line before the next heading
        prefix = "" if h.end == 0 or text[h.end - 1] == "\n" else "\n"
        new_text = text[:h.end] + prefix + body + text[end:]
        return self._commit(rel, text, new_text, dry_run, {"path": rel, "heading": h.text})

    def _commit(self, rel: str, old_text: str, new_text: str, dry_run: bool, info: dict[str, Any]) -> dict[str, Any]:
        """Write *new_text* – or, with dry_run, only report the change as a unified diff."""
        info["changed"] = new_text != old_text
        if dry_run:
            diff = difflib.unified_diff(old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
                                        fromfile=f"a/{rel}", tofile=f"b/{rel}")
            info.update(dry_run=True, diff="".join(diff))
            return info
        if info["changed"]:
            self._write(rel, new_text)
        return info

    def update_frontmatter(self, path: str, set_values: dict[str, Any] | None = None,
                           remove_keys: list[str] | None = None,
                           add_tags: list[str] | None = None, remove_tags: list[str] | None = None) -> dict[str, Any]:
        self._ensure_writable()
        rel = self.note_path(path)
        text = self._abs(rel).read_text(encoding="utf-8")
        fm = parsing.split_frontmatter(text)
        if fm.error:
            raise VaultError(f"Frontmatter kann nicht sicher geändert werden: {fm.error}")
        data = dict(fm.data)
        for k, v in (set_values or {}).items():
            data[k] = v
        for k in remove_keys or []:
            data.pop(k, None)
        if add_tags or remove_tags:
            key = next((k for k in ("tags", "tag", "Tags") if k in data), "tags")
            cur = data.get(key) or []
            if isinstance(cur, str):
                cur = [t for t in re.split(r"[,\s]+", cur) if t]
            cur = [str(t).lstrip("#") for t in cur]
            for t in add_tags or []:
                t = t.lstrip("#")
                if norm(t) not in {norm(c) for c in cur}:
                    cur.append(t)
            drop = {norm(t.lstrip("#")) for t in remove_tags or []}
            cur = [c for c in cur if norm(c) not in drop]
            if cur:
                data[key] = cur
            else:
                data.pop(key, None)
        body = text[fm.end:]
        self._write(rel, parsing.join_frontmatter(data, body))
        return {"path": rel, "frontmatter": jsonable(data)}

    def move_note(self, src: str, dest: str, update_links: bool = True, overwrite: bool = False) -> dict[str, Any]:
        self._ensure_writable()
        old = self.note_path(src)
        dest = dest.strip()
        if dest.endswith("/"):
            dest = dest + old.rsplit("/", 1)[-1]
        new = self.note_path(dest, must_exist=False)
        if norm(new) == norm(old) and new == old:
            raise VaultError("Quelle und Ziel sind identisch.")
        if self._abs(new).exists() and norm(new) != norm(old) and not overwrite:
            raise VaultError(f"Ziel existiert bereits: {new}")

        idx = self.index()
        edits: dict[str, list[tuple[tuple[int, int], str]]] = defaultdict(list)
        if update_links:
            new_stem_key = norm(_strip_md(new.rsplit("/", 1)[-1]))
            others_same_name = [p for p in idx.by_name.get(new_stem_key, []) if p != old]
            new_name_unique = not others_same_name
            for src_rel, ni in self._iter_infos(idx):
                src_after = new if src_rel == old else src_rel
                for link in ni.links:
                    tgt = self.resolve_link(link, src_rel, idx)
                    if tgt is None or not link.target:
                        continue
                    points_to_moved = tgt == old
                    rel_md_in_moved = src_rel == old and link.kind == "markdown"
                    if not (points_to_moved or rel_md_in_moved):
                        continue
                    tgt_after = new if points_to_moved else tgt
                    if link.kind == "wiki":
                        if not points_to_moved:
                            continue
                        had_ext = link.target.lower().endswith(NOTE_EXT)
                        if "/" not in link.target and new_name_unique:
                            repl = new.rsplit("/", 1)[-1]
                        else:
                            repl = new
                        if not had_ext:
                            repl = _strip_md(repl)
                    else:
                        relp = posixpath.relpath(tgt_after, posixpath.dirname(src_after) or ".")
                        if link.bracketed:
                            repl = "<" + relp + link.subpath + ">"
                        else:
                            repl = quote(relp, safe="/") + (quote(link.subpath, safe="#^") if link.subpath else "")
                    edits[src_rel].append((link.target_span, repl))

        # perform move
        self._abs(new).parent.mkdir(parents=True, exist_ok=True)
        os.replace(self._abs(old), self._abs(new))
        with self._lock:
            self._cache.pop(old, None)

        changed = []
        for src_rel, repls in edits.items():
            file_rel = new if src_rel == old else src_rel
            text = self._abs(file_rel).read_text(encoding="utf-8")
            for (start, end), repl in sorted(repls, key=lambda e: e[0][0], reverse=True):
                text = text[:start] + repl + text[end:]
            self._write(file_rel, text)
            changed.append(file_rel)
        # remove empty source folder? keep it – Obsidian also keeps folders.
        return {"from": old, "to": new, "links_updated_in": sorted(changed)}

    def delete_note(self, path: str, permanent: bool = False) -> dict[str, Any]:
        self._ensure_writable()
        rel = self.note_path(path)
        full = self._abs(rel)
        if permanent:
            full.unlink()
            result = {"path": rel, "deleted": "permanent"}
        else:
            trash = self.root / ".trash"
            trash.mkdir(exist_ok=True)
            base = full.stem
            target = trash / full.name
            i = 1
            while target.exists():
                target = trash / f"{base} {i}{full.suffix}"
                i += 1
            os.replace(full, target)
            result = {"path": rel, "deleted": "moved to .trash", "trash_file": f".trash/{target.name}"}
        with self._lock:
            self._cache.pop(rel, None)
        return result

    # ------------------------------------------------------------------ daily notes & templates

    def _obsidian_json(self, name: str) -> dict[str, Any]:
        p = self.root / ".obsidian" / name
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def daily_settings(self) -> dict[str, Any]:
        d = self._obsidian_json("daily-notes.json")
        return {
            "folder": (d.get("folder") or "").strip("/"),
            "format": d.get("format") or "YYYY-MM-DD",
            "template": (d.get("template") or "").strip("/") or None,
        }

    def templates_settings(self) -> dict[str, Any]:
        t = self._obsidian_json("templates.json")
        return {
            "folder": (t.get("folder") or "").strip("/"),
            "date_format": t.get("dateFormat") or "YYYY-MM-DD",
            "time_format": t.get("timeFormat") or "HH:mm",
        }

    def daily_note_path(self, when: str | None = None) -> str:
        s = self.daily_settings()
        d = parse_date_arg(when)
        name = format_moment(s["format"], d, self.cfg.locale)
        rel = f'{s["folder"]}/{name}' if s["folder"] else name
        return self.note_path(rel, must_exist=False)

    def _render_template_file(self, template_rel: str, title: str, variables: dict[str, str] | None,
                              now: datetime | None = None) -> str:
        ts = self.templates_settings()
        raw = self._abs(template_rel).read_text(encoding="utf-8")
        return render_template(raw, title=title, now=now, date_format=ts["date_format"],
                               time_format=ts["time_format"], locale=self.cfg.locale, variables=variables)

    def daily_note(self, when: str | None = None, create: bool = True) -> dict[str, Any]:
        rel = self.daily_note_path(when)
        full = self._abs(rel)
        created = False
        if not full.exists():
            if not create:
                return {"path": rel, "exists": False}
            self._ensure_writable()
            content = ""
            tpl = self.daily_settings()["template"]
            if tpl:
                try:
                    tpl_rel = self.note_path(tpl)
                    d = parse_date_arg(when)
                    now = datetime.combine(d, datetime.now().time())
                    content = self._render_template_file(tpl_rel, _strip_md(rel.rsplit("/", 1)[-1]), None, now)
                except VaultError:
                    content = ""
            self._write(rel, content)
            created = True
        return {"path": rel, "exists": True, "created": created, "content": self.info(rel).text}

    def list_templates(self) -> dict[str, Any]:
        folder = self.templates_settings()["folder"]
        if not folder:
            return {"folder": None, "templates": [],
                    "hint": "Kein Template-Ordner in Obsidian (Core-Plugin 'Vorlagen') konfiguriert."}
        prefix = folder + "/"
        return {"folder": folder, "templates": [p for p in self.index().notes if p.startswith(prefix)]}

    def create_from_template(self, template: str, path: str, variables: dict[str, str] | None = None,
                             overwrite: bool = False) -> dict[str, Any]:
        self._ensure_writable()
        folder = self.templates_settings()["folder"]
        tpl_rel = None
        try:
            tpl_rel = self.note_path(f"{folder}/{template}" if folder and "/" not in template else template)
        except VaultError:
            tpl_rel = self.note_path(template)
        rel = self.note_path(path, must_exist=False)
        title = _strip_md(rel.rsplit("/", 1)[-1])
        content = self._render_template_file(tpl_rel, title, variables)
        res = self.create_note(rel, content, overwrite=overwrite)
        res["template"] = tpl_rel
        return res
