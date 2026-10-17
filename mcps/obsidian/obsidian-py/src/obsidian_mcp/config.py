"""Configuration: vaults from a TOML file, environment variables and CLI flags."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_EXCLUDES = [".obsidian", ".trash", ".git", ".stfolder", ".stversions", "node_modules"]


@dataclass
class VaultConfig:
    name: str
    path: Path
    read_only: bool = False
    locale: str = "de"  # for daily-note names (Monatsnamen etc.)
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))


@dataclass
class ServerConfig:
    vaults: dict[str, VaultConfig] = field(default_factory=dict)
    host: str = "127.0.0.1"
    port: int = 8765
    token: str | None = None
    allowed_hosts: list[str] = field(default_factory=list)
    cf_access_team: str | None = None  # Cloudflare Access team name or domain (e.g. "myteam" or "myteam.cloudflareaccess.com")
    cf_access_aud: str | None = None  # AUD tag of the Access application
    resource_limit: int = 1000  # max. notes listed as MCP resources (newest first)


def _slug(name: str) -> str:
    return re.sub(r"[^\w\-]+", "-", name.strip()).strip("-").lower() or "vault"


def _truthy(v: str | None) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "ja", "on"}


def parse_vault_spec(spec: str, default_read_only: bool = False) -> VaultConfig:
    """Parse 'name=/path' or just '/path' (name = folder name). Suffix ':ro' => read-only."""
    read_only = default_read_only
    if spec.endswith(":ro"):
        spec, read_only = spec[:-3], True
    elif spec.endswith(":rw"):
        spec, read_only = spec[:-3], False
    if "=" in spec and not spec.startswith(("/", "~", ".")):
        name, _, path = spec.partition("=")
    else:
        path = spec
        name = Path(os.path.expanduser(path)).name
    return VaultConfig(name=_slug(name), path=Path(os.path.expanduser(path.strip())).resolve(), read_only=read_only)


def load_config(
    config_file: str | None = None,
    cli_vaults: list[str] | None = None,
    read_only: bool = False,
    host: str | None = None,
    port: int | None = None,
    token: str | None = None,
) -> ServerConfig:
    cfg = ServerConfig()
    config_file = config_file or os.environ.get("OBSIDIAN_MCP_CONFIG")
    if config_file:
        data = tomllib.loads(Path(os.path.expanduser(config_file)).read_text(encoding="utf-8"))
        srv = data.get("server", {})
        cfg.host = srv.get("host", cfg.host)
        cfg.port = int(srv.get("port", cfg.port))
        cfg.token = srv.get("token") or None
        cfg.allowed_hosts = list(srv.get("allowed_hosts", []))
        cfg.cf_access_team = srv.get("cf_access_team") or None
        cfg.cf_access_aud = srv.get("cf_access_aud") or None
        cfg.resource_limit = int(srv.get("resource_limit", cfg.resource_limit))
        for name, v in data.get("vaults", {}).items():
            vc = VaultConfig(
                name=_slug(name),
                path=Path(os.path.expanduser(v["path"])).resolve(),
                read_only=bool(v.get("read_only", False)) or read_only,
                locale=v.get("locale", "de"),
            )
            vc.exclude = list(DEFAULT_EXCLUDES) + list(v.get("exclude", []))
            cfg.vaults[vc.name] = vc

    # OBSIDIAN_VAULTS="privat=/vaults/privat;arbeit=/vaults/arbeit:ro"
    env_vaults = os.environ.get("OBSIDIAN_VAULTS") or os.environ.get("OBSIDIAN_VAULT")
    specs = [s for s in re.split(r"[;\n]", env_vaults or "") if s.strip()]
    specs += cli_vaults or []
    env_ro = _truthy(os.environ.get("OBSIDIAN_READ_ONLY"))
    for spec in specs:
        vc = parse_vault_spec(spec.strip(), default_read_only=read_only or env_ro)
        cfg.vaults[vc.name] = vc

    cfg.host = host or os.environ.get("OBSIDIAN_MCP_HOST") or cfg.host
    cfg.port = int(port or os.environ.get("OBSIDIAN_MCP_PORT") or cfg.port)
    cfg.token = token or os.environ.get("OBSIDIAN_MCP_TOKEN") or cfg.token
    if os.environ.get("OBSIDIAN_MCP_RESOURCE_LIMIT"):
        cfg.resource_limit = int(os.environ["OBSIDIAN_MCP_RESOURCE_LIMIT"])
    if os.environ.get("OBSIDIAN_MCP_ALLOWED_HOSTS"):
        cfg.allowed_hosts = [h.strip() for h in os.environ["OBSIDIAN_MCP_ALLOWED_HOSTS"].split(",") if h.strip()]
    cfg.cf_access_team = os.environ.get("OBSIDIAN_MCP_CF_ACCESS_TEAM") or cfg.cf_access_team
    cfg.cf_access_aud = os.environ.get("OBSIDIAN_MCP_CF_ACCESS_AUD") or cfg.cf_access_aud
    if bool(cfg.cf_access_team) != bool(cfg.cf_access_aud):
        raise SystemExit("Cloudflare Access braucht beides: OBSIDIAN_MCP_CF_ACCESS_TEAM und OBSIDIAN_MCP_CF_ACCESS_AUD.")

    if not cfg.vaults:
        raise SystemExit(
            "Kein Vault konfiguriert. Nutze --vault name=/pfad, OBSIDIAN_VAULTS oder eine Config-Datei (--config)."
        )
    for vc in cfg.vaults.values():
        if not vc.path.is_dir():
            raise SystemExit(f"Vault '{vc.name}': Ordner existiert nicht: {vc.path}")
    return cfg
