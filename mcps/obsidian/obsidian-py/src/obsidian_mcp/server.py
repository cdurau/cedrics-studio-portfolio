"""MCP server exposing one or more Obsidian vaults (stdio + streamable HTTP)."""

from __future__ import annotations

import argparse
import hmac
import logging
import secrets
import sys
import time
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from . import __version__
from .config import ServerConfig, load_config
from .resources import ObsidianMCP, parse_uri
from .vault import Vault, VaultError

log = logging.getLogger("obsidian_mcp")

INSTRUCTIONS = """\
Access to the user's Obsidian vault(s). Notes are Markdown files; paths are relative to the
vault root (".md" is optional). Obsidian wikilinks ([[Note]], [[Note#Heading|Alias]]), tags
(#tag, frontmatter `tags`) and YAML frontmatter are understood.

Tips:
- Call list_vaults first if more than one vault is configured; pass `vault` to every tool.
- Prefer search_notes / find_notes_by_tag / get_backlinks to discover notes, then read_note.
- For edits prefer append_to_note (optionally under a heading), replace_section, replace_in_note or
  update_frontmatter over rewriting whole notes with create_note(overwrite=true).
- replace_in_note and replace_section accept dry_run=true to preview the change as a diff.
- List-type results are JSON objects with a `total` count and a named list (e.g. {"total": 2, "tags": [...]}).
- move_note keeps links intact by rewriting them in all referring notes.
- delete_note moves notes to the vault's .trash folder unless permanent=true.
- Every `path` parameter also accepts obsidian:// URIs, e.g. obsidian://<vault>/Folder/Note.md#Heading
  or Obsidian's own "Copy Obsidian URL" links (obsidian://open?vault=...&file=...).
- Notes are also exposed as MCP resources (obsidian://<vault>/<path>, .daily/<date>, .tag/<tag>).
"""

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)


def listing(key: str, items: list[Any], **extra: Any) -> dict[str, Any]:
    """Uniform envelope for list results: one JSON object instead of one content block per item
    (and no empty result for empty lists)."""
    return {"total": len(items), **extra, key: items}


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

    mcp = ObsidianMCP(
        vaults,
        "obsidian",
        resource_limit=cfg.resource_limit,
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

    def VP(vault: str | None, path: str) -> tuple[Vault, str, str | None]:
        """Vault + path, where path may also be an obsidian:// URI (vault taken from the URI)."""
        if path and path.strip().lower().startswith("obsidian://"):
            p = parse_uri(path.strip(), vaults)
            v = vaults[p.vault]
            if p.kind == "daily":
                return v, v.daily_note_path(p.arg), p.fragment
            if p.kind != "file":
                raise VaultError(f"URI zeigt nicht auf eine Notiz: {path}")
            frag = p.fragment if p.fragment and not p.fragment.startswith("^") else None
            return v, p.arg, frag
        return V(vault), path, None

    # ------------------------------------------------------------------ overview

    @mcp.tool(annotations=READ)
    def list_vaults() -> dict[str, Any]:
        """List configured vaults with note counts, read-only flag and daily-note/template settings."""
        return listing("vaults", [v.summary() for v in vaults.values()])

    @mcp.tool(annotations=READ)
    def list_notes(vault: str | None = None, folder: str | None = None, recursive: bool = True,
                   include_attachments: bool = False, limit: int = 500) -> dict[str, Any]:
        """List notes (and optionally attachments) in the vault or a folder, plus its direct subfolders."""
        return V(vault).list_notes(folder, recursive, include_attachments, limit)

    @mcp.tool(annotations=READ)
    def list_folders(vault: str | None = None) -> dict[str, Any]:
        """List all folders of the vault (hidden folders like .obsidian/.trash are excluded)."""
        return listing("folders", V(vault).list_folders())

    @mcp.tool(annotations=READ)
    def get_recent_notes(vault: str | None = None, limit: int = 20, folder: str | None = None) -> dict[str, Any]:
        """Most recently modified notes, newest first."""
        return listing("notes", V(vault).recent(limit, folder))

    # ------------------------------------------------------------------ reading & search

    @mcp.tool(annotations=READ)
    def read_note(path: str, vault: str | None = None, section: str | None = None,
                  include_metadata: bool = True) -> dict[str, Any]:
        """Read a note. `path` may be 'Folder/Note', 'Note.md' or just a unique note name.
        `section` returns only the part under that heading. Metadata includes frontmatter,
        tags, headings, size and modification time."""
        v, path, frag = VP(vault, path)
        return v.read_note(path, section or frag, include_metadata)

    @mcp.tool(annotations=READ)
    def read_multiple_notes(paths: list[str], vault: str | None = None,
                            max_chars_each: int = 20000) -> dict[str, Any]:
        """Read several notes at once (content only)."""
        out = []
        for p in paths:
            try:
                v, rel, _ = VP(vault, p)
                out += v.read_many([rel], max_chars_each)
            except VaultError as exc:
                out.append({"path": p, "error": str(exc)})
        return listing("notes", out)

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
                              folder: str | None = None, limit: int = 100) -> dict[str, Any]:
        """Find notes whose frontmatter has `key` (and, if given, a value equal to/containing `value`;
        list values are checked element-wise, case-insensitive)."""
        return listing("notes", V(vault).search_frontmatter(key, value, folder, limit), key=key)

    # ------------------------------------------------------------------ links & tags

    @mcp.tool(annotations=READ)
    def get_outgoing_links(path: str, vault: str | None = None) -> dict[str, Any]:
        """Links and embeds in a note, each with the resolved target path (null = unresolved)."""
        v, path, _ = VP(vault, path)
        return listing("links", v.outgoing_links(path), path=v.note_path(path))

    @mcp.tool(annotations=READ)
    def get_backlinks(path: str, vault: str | None = None) -> dict[str, Any]:
        """Notes that link to or embed the given note, with line context."""
        v, path, _ = VP(vault, path)
        return listing("backlinks", v.backlinks(path), path=v.note_path(path))

    @mcp.tool(annotations=READ)
    def list_tags(vault: str | None = None, prefix: str | None = None) -> dict[str, Any]:
        """All tags (inline #tags and frontmatter tags) with the number of notes using them."""
        return listing("tags", V(vault).tags(prefix))

    @mcp.tool(annotations=READ)
    def find_notes_by_tag(tag: str, vault: str | None = None, include_nested: bool = True) -> dict[str, Any]:
        """Notes carrying a tag ('#projekt' or 'projekt'). include_nested also matches #projekt/sub."""
        return listing("notes", V(vault).notes_by_tag(tag, include_nested), tag=tag)

    @mcp.tool(annotations=READ)
    def find_orphan_notes(vault: str | None = None, strict: bool = False) -> dict[str, Any]:
        """Notes without incoming links. strict=true: only notes with neither incoming nor outgoing links."""
        return listing("notes", V(vault).orphans(strict))

    @mcp.tool(annotations=READ)
    def find_unresolved_links(vault: str | None = None) -> dict[str, Any]:
        """Links pointing to notes/files that don't exist, grouped by target."""
        return listing("unresolved", V(vault).unresolved())

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
        v, path, frag = VP(vault, path)
        return v.append(path, content, heading=heading or frag, create_if_missing=create_if_missing, prepend=prepend)

    @mcp.tool(annotations=WRITE)
    def replace_in_note(path: str, old: str, new: str, vault: str | None = None, regex: bool = False,
                        count: int = 0, dry_run: bool = False) -> dict[str, Any]:
        """Replace text in a note (count=0 replaces all occurrences). Errors if nothing matched.
        dry_run=true writes nothing and returns the change as a unified diff."""
        v, path, _ = VP(vault, path)
        return v.replace_in_note(path, old, new, regex, count, dry_run)

    @mcp.tool(annotations=DESTRUCTIVE)
    def replace_section(path: str, content: str, heading: str | None = None, vault: str | None = None,
                        dry_run: bool = False) -> dict[str, Any]:
        """Replace the whole content below a heading (up to the next heading of the same or higher
        level). The heading line itself is kept; `content` is the new body without the heading.
        `heading` may also be given as #fragment of an obsidian:// URI.
        dry_run=true writes nothing and returns the change as a unified diff."""
        v, path, frag = VP(vault, path)
        if not (heading or frag):
            raise VaultError("Bitte `heading` angeben.")
        return v.replace_section(path, heading or frag, content, dry_run)

    @mcp.tool(annotations=WRITE)
    def update_frontmatter(path: str, vault: str | None = None, set: dict[str, Any] | None = None,  # noqa: A002
                           remove: list[str] | None = None, add_tags: list[str] | None = None,
                           remove_tags: list[str] | None = None) -> dict[str, Any]:
        """Set/remove frontmatter properties and add/remove tags without touching the note body."""
        v, path, _ = VP(vault, path)
        return v.update_frontmatter(path, set, remove, add_tags, remove_tags)

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
        v, source, _ = VP(vault, source)
        return v.move_note(source, destination, update_links, overwrite)

    @mcp.tool(annotations=DESTRUCTIVE)
    def delete_note(path: str, vault: str | None = None, permanent: bool = False) -> dict[str, Any]:
        """Delete a note: moved to the vault's .trash folder by default, permanent=true deletes it."""
        v, path, _ = VP(vault, path)
        return v.delete_note(path, permanent)

    return _finish(mcp)


def _finish(mcp: FastMCP) -> FastMCP:
    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request):  # noqa: ANN001
        from starlette.responses import JSONResponse
        return JSONResponse({"status": "ok", "version": __version__})

    return mcp


# ---------------------------------------------------------------------- HTTP auth


class CfAccessVerifier:
    """Validates the 'Cf-Access-Jwt-Assertion' header that Cloudflare Access adds to every request it lets through.
    Signature (team JWKS), audience (AUD tag of the Access app), issuer and expiry are checked."""

    def __init__(self, team: str, aud: str, jwks_client: Any = None):
        import jwt

        domain = team if "." in team else f"{team}.cloudflareaccess.com"
        self.issuer = f"https://{domain}"
        self.aud = aud
        self.jwks = jwks_client or jwt.PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs", lifespan=3600)

    def verify(self, token: str) -> dict:
        import jwt

        key = self.jwks.get_signing_key_from_jwt(token).key
        return jwt.decode(token, key, algorithms=["RS256"], audience=self.aud, issuer=self.issuer,
                          options={"require": ["exp", "iat", "aud", "iss"]})


def _expiry_hint(assertion: bytes) -> str:
    """Diagnostics only: ' (exp vor 123s, iat vor 456s)' from the unverified payload of a rejected JWT."""
    import jwt

    try:
        c = jwt.decode(assertion, options={"verify_signature": False, "verify_exp": False, "verify_aud": False})
        now = int(time.time())
        return f" (exp {now - int(c['exp']):+d}s, iat {now - int(c['iat']):+d}s relativ zu jetzt)"
    except Exception:
        return ""


class BearerAuth:
    """Minimal ASGI middleware: accepts 'Authorization: Bearer <token>' and/or a valid Cloudflare Access JWT
    ('Cf-Access-Jwt-Assertion'); any configured method that succeeds lets the request through.
    Tokens in the query string are deliberately not accepted (they end up in logs and histories)."""

    def __init__(self, app, token: str | None = None, cf_access: CfAccessVerifier | None = None,
                 open_paths: tuple[str, ...] = ("/health",)):
        self.app = app
        self.token = token.encode() if token else None
        self.cf_access = cf_access
        self.open_paths = open_paths

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in self.open_paths:
            return await self.app(scope, receive, send)
        supplied = b""
        assertion = b""
        for k, v in scope.get("headers", []):
            if k == b"authorization" and v[:7].lower() == b"bearer ":
                supplied = v[7:].strip()
            elif k == b"cf-access-jwt-assertion":
                assertion = v.strip()
        if self.token and supplied and hmac.compare_digest(supplied, self.token):
            return await self.app(scope, receive, send)
        if self.cf_access and assertion:
            import anyio

            try:
                await anyio.to_thread.run_sync(self.cf_access.verify, assertion.decode("ascii"))
                return await self.app(scope, receive, send)
            except Exception as e:  # invalid/expired token, wrong AUD, JWKS unreachable, …
                log.warning("Cloudflare-Access-JWT abgelehnt: %s%s", type(e).__name__, _expiry_hint(assertion))
        body = b'{"error":"unauthorized"}'
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"),
                                (b"www-authenticate", b"Bearer"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def build_http_app(cfg: ServerConfig):
    mcp = build_server(cfg)
    app = mcp.streamable_http_app()
    cf_access = CfAccessVerifier(cfg.cf_access_team, cfg.cf_access_aud) if cfg.cf_access_team else None
    return BearerAuth(app, cfg.token, cf_access) if cfg.token or cf_access else app


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
    if not cfg.token and not cfg.cf_access_team and not local and not args.no_auth:
        raise SystemExit(
            f"HTTP auf {cfg.host} ohne Token ist unsicher. Setze OBSIDIAN_MCP_TOKEN "
            "(z. B. via `obsidian-mcp --gen-token`) oder nutze --no-auth bewusst."
        )
    import uvicorn

    auth = [name for name, on in (("Bearer", cfg.token), ("Cloudflare Access", cfg.cf_access_team)) if on]
    log.info("HTTP-Endpoint: http://%s:%s/mcp  (Auth: %s)", cfg.host, cfg.port, " + ".join(auth) or "keine")
    uvicorn.run(build_http_app(cfg), host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
