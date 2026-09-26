"""MCP server exposing one or more Obsidian vaults (stdio + streamable HTTP)."""

from __future__ import annotations

import argparse
import hmac
import logging
import secrets
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from . import __version__
from .config import ServerConfig, load_config
from .vault import Vault, VaultError

log = logging.getLogger("obsidian_mcp")

INSTRUCTIONS = """\
Access to the user's Obsidian vault(s). Notes are Markdown files; paths are relative to the
vault root (".md" is optional). Obsidian wikilinks ([[Note]], [[Note#Heading|Alias]]), tags
(#tag, frontmatter `tags`) and YAML frontmatter are understood.

Tips:
- Call list_vaults first if more than one vault is configured; pass `vault` to every tool.
- Prefer search_notes / find_notes_by_tag / get_backlinks to discover notes, then read_note.
- For edits prefer append_to_note (optionally under a heading), replace_in_note or
  update_frontmatter over rewriting whole notes with create_note(overwrite=true).
- move_note keeps links intact by rewriting them in all referring notes.
- delete_note moves notes to the vault's .trash folder unless permanent=true.
"""

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)


def build_server(cfg: ServerConfig) -> FastMCP:
    vaults = {name: Vault(vc) for name, vc in cfg.vaults.items()}
    default_vault = next(iter(vaults)) if len(vaults) == 1 else None
    any_writable = any(not v.read_only for v in vaults.values())

    if cfg.allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=cfg.allowed_hosts,
            allowed_origins=[f"http://{h}" for h in cfg.allowed_hosts] + [f"https://{h}" for h in cfg.allowed_hosts],
        )
    else:
        # Protection comes from the bearer token; host header checks would break Tailscale/LAN names.
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

    mcp = FastMCP(
        "obsidian",
        instructions=INSTRUCTIONS,
        host=cfg.host,
        port=cfg.port,
        stateless_http=True,
        transport_security=security,
    )

    def V(name: str | None) -> Vault:
        if name:
            key = name.strip().lower()
            if key in vaults:
                return vaults[key]
            raise VaultError(f"Unbekannter Vault '{name}'. Verfügbar: {', '.join(vaults)}")
        if default_vault:
            return vaults[default_vault]
        raise VaultError(f"Bitte `vault` angeben. Verfügbar: {', '.join(vaults)}")

    # ------------------------------------------------------------------ overview

    @mcp.tool(annotations=READ)
    def list_vaults() -> list[dict[str, Any]]:
        """List configured vaults with note counts, read-only flag and daily-note/template settings."""
        return [v.summary() for v in vaults.values()]

    @mcp.tool(annotations=READ)
    def list_notes(vault: str | None = None, folder: str | None = None, recursive: bool = True,
                   include_attachments: bool = False, limit: int = 500) -> dict[str, Any]:
        """List notes (and optionally attachments) in the vault or a folder, plus its direct subfolders."""
        return V(vault).list_notes(folder, recursive, include_attachments, limit)

    @mcp.tool(annotations=READ)
    def list_folders(vault: str | None = None) -> list[str]:
        """List all folders of the vault (hidden folders like .obsidian/.trash are excluded)."""
        return V(vault).list_folders()

    @mcp.tool(annotations=READ)
    def get_recent_notes(vault: str | None = None, limit: int = 20, folder: str | None = None) -> list[dict[str, Any]]:
        """Most recently modified notes, newest first."""
        return V(vault).recent(limit, folder)

    # ------------------------------------------------------------------ reading & search

    @mcp.tool(annotations=READ)
    def read_note(path: str, vault: str | None = None, section: str | None = None,
                  include_metadata: bool = True) -> dict[str, Any]:
        """Read a note. `path` may be 'Folder/Note', 'Note.md' or just a unique note name.
        `section` returns only the part under that heading. Metadata includes frontmatter,
        tags, headings, size and modification time."""
        return V(vault).read_note(path, section, include_metadata)

    @mcp.tool(annotations=READ)
    def read_multiple_notes(paths: list[str], vault: str | None = None,
                            max_chars_each: int = 20000) -> list[dict[str, Any]]:
        """Read several notes at once (content only)."""
        return V(vault).read_many(paths, max_chars_each)

    @mcp.tool(annotations=READ)
    def search_notes(query: str, vault: str | None = None, regex: bool = False, case_sensitive: bool = False,
                     folder: str | None = None, limit: int = 20, snippets_per_note: int = 3) -> dict[str, Any]:
        """Full-text search over note contents and paths. Plain queries match notes containing ALL
        words ("quoted phrases" supported); set regex=true for a regular expression. Returns
        ranked notes with line snippets."""
        return V(vault).search(query, regex=regex, case_sensitive=case_sensitive, folder=folder,
                               limit=limit, snippets_per_note=snippets_per_note)

    @mcp.tool(annotations=READ)
    def search_by_frontmatter(key: str, value: str | None = None, vault: str | None = None,
                              folder: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Find notes whose frontmatter has `key` (and, if given, a value equal to/containing `value`;
        list values are checked element-wise, case-insensitive)."""
        return V(vault).search_frontmatter(key, value, folder, limit)

    # ------------------------------------------------------------------ links & tags

    @mcp.tool(annotations=READ)
    def get_outgoing_links(path: str, vault: str | None = None) -> list[dict[str, Any]]:
        """Links and embeds in a note, each with the resolved target path (null = unresolved)."""
        return V(vault).outgoing_links(path)

    @mcp.tool(annotations=READ)
    def get_backlinks(path: str, vault: str | None = None) -> list[dict[str, Any]]:
        """Notes that link to or embed the given note, with line context."""
        return V(vault).backlinks(path)

    @mcp.tool(annotations=READ)
    def list_tags(vault: str | None = None, prefix: str | None = None) -> list[dict[str, Any]]:
        """All tags (inline #tags and frontmatter tags) with the number of notes using them."""
        return V(vault).tags(prefix)

    @mcp.tool(annotations=READ)
    def find_notes_by_tag(tag: str, vault: str | None = None, include_nested: bool = True) -> list[str]:
        """Notes carrying a tag ('#projekt' or 'projekt'). include_nested also matches #projekt/sub."""
        return V(vault).notes_by_tag(tag, include_nested)

    @mcp.tool(annotations=READ)
    def find_orphan_notes(vault: str | None = None, strict: bool = False) -> list[str]:
        """Notes without incoming links. strict=true: only notes with neither incoming nor outgoing links."""
        return V(vault).orphans(strict)

    @mcp.tool(annotations=READ)
    def find_unresolved_links(vault: str | None = None) -> list[dict[str, Any]]:
        """Links pointing to notes/files that don't exist, grouped by target."""
        return V(vault).unresolved()

    # ------------------------------------------------------------------ daily notes & templates (read)

    @mcp.tool(annotations=READ)
    def list_templates(vault: str | None = None) -> dict[str, Any]:
        """Templates in the folder configured in Obsidian's core Templates plugin."""
        return V(vault).list_templates()

    if not any_writable:
        @mcp.tool(annotations=READ)
        def get_daily_note(vault: str | None = None, date: str | None = None) -> dict[str, Any]:
            """Read the daily note for a date (YYYY-MM-DD, today/heute, yesterday/gestern, -1, +1)."""
            return V(vault).daily_note(date, create=False)
        return _finish(mcp)

    # ------------------------------------------------------------------ writing

    @mcp.tool(annotations=WRITE)
    def get_daily_note(vault: str | None = None, date: str | None = None, create: bool = True) -> dict[str, Any]:
        """Get (and by default create) the daily note for a date. Uses Obsidian's Daily Notes
        settings (folder, date format, template). date: YYYY-MM-DD, today/heute,
        yesterday/gestern, tomorrow/morgen or offsets like -1/+2. Default: today."""
        return V(vault).daily_note(date, create=create and not V(vault).read_only)

    @mcp.tool(annotations=WRITE)
    def append_to_daily_note(content: str, vault: str | None = None, date: str | None = None,
                             heading: str | None = None) -> dict[str, Any]:
        """Append text to the daily note (created from template if missing), optionally under a heading."""
        v = V(vault)
        v.daily_note(date, create=True)
        return v.append(v.daily_note_path(date), content, heading=heading)

    @mcp.tool(annotations=WRITE)
    def create_note(path: str, content: str = "", vault: str | None = None,
                    frontmatter: dict[str, Any] | None = None, overwrite: bool = False) -> dict[str, Any]:
        """Create a note (folders are created as needed). `frontmatter` is merged into any YAML
        already present in `content`. Fails if the note exists unless overwrite=true."""
        return V(vault).create_note(path, content, frontmatter, overwrite)

    @mcp.tool(annotations=WRITE)
    def append_to_note(path: str, content: str, vault: str | None = None, heading: str | None = None,
                       prepend: bool = False, create_if_missing: bool = True) -> dict[str, Any]:
        """Append text to a note. With `heading`, insert at the end of that section (heading is
        created at the end if missing). prepend=true inserts at the start (after frontmatter or
        directly below the heading)."""
        return V(vault).append(path, content, heading=heading, create_if_missing=create_if_missing, prepend=prepend)

    @mcp.tool(annotations=WRITE)
    def replace_in_note(path: str, old: str, new: str, vault: str | None = None, regex: bool = False,
                        count: int = 0) -> dict[str, Any]:
        """Replace text in a note (count=0 replaces all occurrences). Errors if nothing matched."""
        return V(vault).replace_in_note(path, old, new, regex, count)

    @mcp.tool(annotations=WRITE)
    def update_frontmatter(path: str, vault: str | None = None, set: dict[str, Any] | None = None,  # noqa: A002
                           remove: list[str] | None = None, add_tags: list[str] | None = None,
                           remove_tags: list[str] | None = None) -> dict[str, Any]:
        """Set/remove frontmatter properties and add/remove tags without touching the note body."""
        return V(vault).update_frontmatter(path, set, remove, add_tags, remove_tags)

    @mcp.tool(annotations=WRITE)
    def create_note_from_template(template: str, path: str, vault: str | None = None,
                                  variables: dict[str, str] | None = None, overwrite: bool = False) -> dict[str, Any]:
        """Create a note from a template. Supports {{title}}, {{date}}, {{time}}, {{date:FORMAT}},
        {{time:FORMAT}} and custom {{variables}}. (Templater <% %> syntax is not executed.)"""
        return V(vault).create_from_template(template, path, variables, overwrite)

    @mcp.tool(annotations=DESTRUCTIVE)
    def move_note(source: str, destination: str, vault: str | None = None, update_links: bool = True,
                  overwrite: bool = False) -> dict[str, Any]:
        """Move/rename a note. Destination ending in '/' keeps the file name. With update_links
        all wikilinks and markdown links pointing to it are rewritten (like Obsidian does)."""
        return V(vault).move_note(source, destination, update_links, overwrite)

    @mcp.tool(annotations=DESTRUCTIVE)
    def delete_note(path: str, vault: str | None = None, permanent: bool = False) -> dict[str, Any]:
        """Delete a note: moved to the vault's .trash folder by default, permanent=true deletes it."""
        return V(vault).delete_note(path, permanent)

    return _finish(mcp)


def _finish(mcp: FastMCP) -> FastMCP:
    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request):  # noqa: ANN001
        from starlette.responses import JSONResponse
        return JSONResponse({"status": "ok", "version": __version__})

    return mcp


# ---------------------------------------------------------------------- HTTP auth


class BearerAuth:
    """Minimal ASGI middleware: requires 'Authorization: Bearer <token>' (or ?token=)."""

    def __init__(self, app, token: str, open_paths: tuple[str, ...] = ("/health",)):
        self.app = app
        self.token = token.encode()
        self.open_paths = open_paths

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in self.open_paths:
            return await self.app(scope, receive, send)
        supplied = b""
        for k, v in scope.get("headers", []):
            if k == b"authorization" and v[:7].lower() == b"bearer ":
                supplied = v[7:].strip()
                break
        if not supplied:
            from urllib.parse import parse_qs
            qs = parse_qs(scope.get("query_string", b"").decode())
            supplied = (qs.get("token") or [""])[0].encode()
        if supplied and hmac.compare_digest(supplied, self.token):
            return await self.app(scope, receive, send)
        body = b'{"error":"unauthorized"}'
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"),
                                (b"www-authenticate", b"Bearer"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def build_http_app(cfg: ServerConfig):
    mcp = build_server(cfg)
    app = mcp.streamable_http_app()
    return BearerAuth(app, cfg.token) if cfg.token else app


# ---------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="obsidian-mcp", description="MCP server for Obsidian vaults")
    p.add_argument("--config", "-c", help="TOML config file (or env OBSIDIAN_MCP_CONFIG)")
    p.add_argument("--vault", "-v", action="append", default=[],
                   help="Vault as name=/path or /path (repeatable). Suffix :ro for read-only.")
    p.add_argument("--read-only", action="store_true", help="All vaults read-only")
    p.add_argument("--transport", "-t", choices=["stdio", "http"], default="stdio")
    p.add_argument("--host", help="HTTP bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, help="HTTP port (default 8765)")
    p.add_argument("--token", help="Bearer token for HTTP (or env OBSIDIAN_MCP_TOKEN)")
    p.add_argument("--no-auth", action="store_true", help="Allow HTTP without token on non-local addresses")
    p.add_argument("--gen-token", action="store_true", help="Print a random token and exit")
    p.add_argument("--version", action="version", version=f"obsidian-mcp {__version__}")
    args = p.parse_args(argv)

    if args.gen_token:
        print(secrets.token_urlsafe(32))
        return

    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config, args.vault, args.read_only, args.host, args.port, args.token)
    for v in cfg.vaults.values():
        log.info("Vault '%s' -> %s%s", v.name, v.path, " (read-only)" if v.read_only else "")

    if args.transport == "stdio":
        build_server(cfg).run("stdio")
        return

    local = cfg.host in ("127.0.0.1", "localhost", "::1")
    if not cfg.token and not local and not args.no_auth:
        raise SystemExit(
            f"HTTP auf {cfg.host} ohne Token ist unsicher. Setze OBSIDIAN_MCP_TOKEN "
            "(z. B. via `obsidian-mcp --gen-token`) oder nutze --no-auth bewusst."
        )
    import uvicorn

    log.info("HTTP-Endpoint: http://%s:%s/mcp  (Auth: %s)", cfg.host, cfg.port, "Bearer" if cfg.token else "keine")
    uvicorn.run(build_http_app(cfg), host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
