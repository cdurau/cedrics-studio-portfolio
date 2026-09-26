"""MCP resources: every note (and attachment) is addressable as an obsidian:// URI.

URI schema (vault = configured vault name):

    obsidian://<vault>/                     vault overview + root folder listing
    obsidian://<vault>/<folder>/            folder listing (trailing slash)
    obsidian://<vault>/<path>.md            note content (text/markdown)
    obsidian://<vault>/<path>.md#Heading    only that section
    obsidian://<vault>/<attachment.png>     attachment (binary)
    obsidian://<vault>/.daily/<date>        daily note (today/heute, gestern, 2026-09-26, -1 …)
    obsidian://<vault>/.tag/<tag>           notes carrying a tag (incl. nested tags)

Obsidian's own links ("Copy Obsidian URL") are accepted as well:

    obsidian://open?vault=<Vault-Name>&file=<Pfad/zur/Notiz>
"""

from __future__ import annotations

import mimetypes
import posixpath
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, unquote, urlsplit

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ResourceError
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.types import Resource as MCPResource
from mcp.types import ResourceTemplate as MCPResourceTemplate

from .parsing import norm
from .vault import NOTE_EXT, Vault, VaultError

SCHEME = "obsidian"
MAX_BINARY = 10 * 1024 * 1024
MD = "text/markdown"


def note_uri(vault: str, rel: str, fragment: str | None = None) -> str:
    uri = f"{SCHEME}://{vault}/{quote(rel, safe='/')}"
    return uri + ("#" + quote(fragment, safe="") if fragment else "")


@dataclass
class ParsedUri:
    vault: str
    kind: str  # "vault" | "folder" | "file" | "daily" | "tag"
    arg: str  # path / date / tag
    fragment: str | None = None


def parse_uri(uri: str, vaults: dict[str, Vault]) -> ParsedUri:
    parts = urlsplit(str(uri))
    if parts.scheme.lower() != SCHEME:
        raise VaultError(f"Keine obsidian://-URI: {uri}")
    fragment = unquote(parts.fragment) or None

    # Obsidian's native form: obsidian://open?vault=Name&file=Path
    if parts.netloc.lower() == "open" and "vault=" in parts.query:
        q = parse_qs(parts.query)
        name = (q.get("vault") or [""])[0]
        file = (q.get("file") or [""])[0]
        vault = _find_vault(name, vaults)
        if not file:
            return ParsedUri(vault, "vault", "")
        file, _, frag = file.partition("#")
        return ParsedUri(vault, "file", file, unquote(frag) or fragment)

    vault = _find_vault(unquote(parts.netloc), vaults)
    path = unquote(parts.path).lstrip("/")
    if path in ("", "/"):
        return ParsedUri(vault, "vault", "")
    head, _, rest = path.partition("/")
    if head == ".daily":
        return ParsedUri(vault, "daily", rest.strip("/") or "today", fragment)
    if head == ".tag":
        return ParsedUri(vault, "tag", rest.strip("/"))
    if path.endswith("/"):
        return ParsedUri(vault, "folder", path.strip("/"))
    return ParsedUri(vault, "file", path, fragment)


def _find_vault(name: str, vaults: dict[str, Vault]) -> str:
    key = name.strip().lower()
    if key in vaults:
        return key
    # match Obsidian vault name = folder name, e.g. "Example Vault Name"
    for k, v in vaults.items():
        if norm(v.root.name) == norm(name.strip()):
            return k
    raise VaultError(f"Unbekannter Vault '{name}'. Verfügbar: {', '.join(vaults)}")


def _listing(vault: Vault, folder: str) -> str:
    res = vault.list_notes(folder or None, recursive=False, include_attachments=True, limit=1000)
    title = f"Ordner `{folder}`" if folder else f"Vault `{vault.name}`"
    lines = [f"# {title}", ""]
    if not folder:
        s = vault.summary()
        lines += [f"{s['notes']} Notizen, {s['attachments']} Anhänge, {s['folders']} Ordner"
                  + (" · schreibgeschützt" if s["read_only"] else ""), ""]
    if res["subfolders"]:
        lines += ["## Unterordner"]
        lines += [f"- [{posixpath.basename(f)}/]({note_uri(vault.name, f)}/)" for f in res["subfolders"]]
        lines.append("")
    notes = [f for f in res["files"] if f.lower().endswith(NOTE_EXT)]
    other = [f for f in res["files"] if not f.lower().endswith(NOTE_EXT)]
    if notes:
        lines += ["## Notizen"]
        lines += [f"- [{posixpath.basename(f)[:-3]}]({note_uri(vault.name, f)})" for f in notes]
        lines.append("")
    if other:
        lines += ["## Anhänge"]
        lines += [f"- [{posixpath.basename(f)}]({note_uri(vault.name, f)})" for f in other]
        lines.append("")
    if res["truncated"]:
        lines.append(f"_… gekürzt, insgesamt {res['total']} Dateien._")
    return "\n".join(lines).rstrip() + "\n"


class ObsidianMCP(FastMCP):
    """FastMCP with dynamic obsidian:// resources for all configured vaults."""

    def __init__(self, vaults: dict[str, Vault], *args, resource_limit: int = 1000, **kwargs):
        self.vaults = vaults
        self.resource_limit = resource_limit
        super().__init__(*args, **kwargs)

    # ------------------------------------------------------------------ listing

    async def list_resources(self) -> list[MCPResource]:
        out = await super().list_resources()
        per_vault = max(1, self.resource_limit // max(1, len(self.vaults)))
        for name, v in self.vaults.items():
            out.append(MCPResource(uri=f"{SCHEME}://{name}/", name=f"{name}: Übersicht",
                                   description=f"Vault {name} – Überblick und Ordnerinhalt", mimeType=MD))
            out.append(MCPResource(uri=f"{SCHEME}://{name}/.daily/today", name=f"{name}: Daily Note heute",
                                   description="Tagesnotiz von heute (falls vorhanden)", mimeType=MD))
            try:
                recent = v.recent(per_vault)
            except (OSError, VaultError):
                continue
            for item in recent:  # newest first – most useful for attaching in clients
                rel = item["path"]
                out.append(MCPResource(
                    uri=note_uri(name, rel),
                    name=posixpath.basename(rel)[:-3],
                    title=posixpath.basename(rel)[:-3],
                    description=f"{name}: {rel} (geändert {item['modified']})",
                    mimeType=MD,
                ))
        return out

    async def list_resource_templates(self) -> list[MCPResourceTemplate]:
        out = await super().list_resource_templates()
        out += [
            MCPResourceTemplate(uriTemplate=f"{SCHEME}://{{vault}}/{{+path}}", name="note",
                                description="Notiz oder Anhang im Vault (Pfad relativ, optional #Überschrift)",
                                mimeType=MD),
            MCPResourceTemplate(uriTemplate=f"{SCHEME}://{{vault}}/{{+folder}}/", name="folder",
                                description="Ordnerinhalt als Markdown-Liste", mimeType=MD),
            MCPResourceTemplate(uriTemplate=f"{SCHEME}://{{vault}}/.daily/{{date}}", name="daily-note",
                                description="Daily Note: today/heute, gestern, morgen, YYYY-MM-DD oder -1/+1",
                                mimeType=MD),
            MCPResourceTemplate(uriTemplate=f"{SCHEME}://{{vault}}/.tag/{{+tag}}", name="tag",
                                description="Alle Notizen mit einem Tag (inkl. verschachtelter Tags)",
                                mimeType=MD),
        ]
        return out

    # ------------------------------------------------------------------ reading

    async def read_resource(self, uri) -> Iterable[ReadResourceContents]:  # noqa: ANN001
        if urlsplit(str(uri)).scheme.lower() != SCHEME:
            return await super().read_resource(uri)
        try:
            return [self.read_obsidian_uri(str(uri))]
        except (VaultError, OSError) as exc:
            raise ResourceError(str(exc)) from exc

    def read_obsidian_uri(self, uri: str) -> ReadResourceContents:
        p = parse_uri(uri, self.vaults)
        v = self.vaults[p.vault]
        if p.kind == "vault":
            return ReadResourceContents(_listing(v, ""), MD)
        if p.kind == "folder":
            return ReadResourceContents(_listing(v, v.folder_path(p.arg)), MD)
        if p.kind == "tag":
            notes = v.notes_by_tag(p.arg)
            lines = [f"# Tag #{p.arg.lstrip('#')}", "", f"{len(notes)} Notizen", ""]
            lines += [f"- [{posixpath.basename(n)[:-3]}]({note_uri(v.name, n)})" for n in notes]
            return ReadResourceContents("\n".join(lines) + "\n", MD)
        if p.kind == "daily":
            res = v.daily_note(p.arg, create=False)
            if not res.get("exists"):
                raise VaultError(f"Daily Note {res['path']} existiert nicht (Tool get_daily_note legt sie an).")
            return self._note(v, res["path"], p.fragment)
        # file
        if p.arg.lower().endswith(NOTE_EXT) or "." not in posixpath.basename(p.arg):
            return self._note(v, p.arg, p.fragment)
        rel = v.file_path(p.arg)
        full = v._abs(rel)
        if full.stat().st_size > MAX_BINARY:
            raise VaultError(f"Datei zu groß für Resource ({full.stat().st_size} Bytes): {rel}")
        mime = mimetypes.guess_type(rel)[0] or "application/octet-stream"
        if mime.startswith("text/") or rel.lower().endswith((".canvas", ".json", ".base")):
            return ReadResourceContents(full.read_text(encoding="utf-8", errors="replace"), mime)
        return ReadResourceContents(full.read_bytes(), mime)

    @staticmethod
    def _note(v: Vault, path: str, fragment: str | None) -> ReadResourceContents:
        section = fragment if fragment and not fragment.startswith("^") else None  # block refs -> whole note
        note = v.read_note(path, section=section, include_metadata=False)
        return ReadResourceContents(note["content"], MD, meta={"vault": v.name, "path": note["path"]})
