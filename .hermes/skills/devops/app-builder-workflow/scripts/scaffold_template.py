#!/usr/bin/env python3
"""
scaffold_template.py — Deterministic Template → App Scaffolding for Portable App Builder Core.

Agent-neutral. Copies exactly one approved profile template from the planning repo
into apps/<slug>/ of the runtime repo, generates app.yaml, and enforces bounded scope.

stdlib-only Python. Fail-closed. No cross-repo manipulation beyond the approved paths.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Set, Tuple

# Canonical profile list — must match select_profile.py SUPPORTED_PROFILES
SUPPORTED_PROFILES: Tuple[str, ...] = (
    "static-vite",
    "react-hono-sqlite",
    "react-hono-postgres",
    "next-postgres",
    "python-fastapi",
    "python-service",
)

# Slug regex: lowercase alphanumeric + hyphens, must start/end with alnum, 2-48 chars
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,46}[a-z0-9]$")

# Directories to exclude from template copy (build artifacts, caches, VCS, test venvs)
EXCLUDED_DIR_NAMES: Set[str] = {
    "node_modules",
    "node_modules_bak",
    "__pycache__",
    ".pytest_cache",
    ".git",
    ".next",
    "dist",
    "build",
    ".venv",
    "venv",
    "venv_test",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".cache",
}

# File name patterns to exclude
EXCLUDED_FILE_PATTERNS: Tuple[str, ...] = (
    "*.pyc",
    "*.pyo",
    ".DS_Store",
    "Thumbs.db",
    "*.egg-info",
)


@dataclass(frozen=True)
class ScaffoldRequest:
    profile: str
    slug: str
    planning_repo: Path
    runtime_repo: Path
    allow_overwrite: bool = False

    @property
    def source_dir(self) -> Path:
        return self.planning_repo / "templates" / self.profile

    @property
    def target_dir(self) -> Path:
        return self.runtime_repo / "apps" / self.slug


def validate_slug(slug: str) -> Tuple[bool, str]:
    """Validate slug format. Returns (ok, error_message)."""
    if not slug:
        return False, "slug is empty"
    if len(slug) < 2:
        return False, f"slug '{slug}' is too short (min 2 chars)"
    if len(slug) > 48:
        return False, f"slug '{slug}' is too long (max 48 chars)"
    if not SLUG_PATTERN.match(slug):
        return False, (
            f"slug '{slug}' is invalid — must be lowercase alphanumeric + hyphens, "
            "start and end with alnum, 2-48 chars"
        )
    if "--" in slug:
        return False, f"slug '{slug}' must not contain consecutive hyphens"
    return True, ""


def validate_profile(profile: str) -> Tuple[bool, str]:
    """Validate profile name. Returns (ok, error_message)."""
    if profile not in SUPPORTED_PROFILES:
        return False, (
            f"profile '{profile}' is not supported. "
            f"Supported: {', '.join(SUPPORTED_PROFILES)}"
        )
    return True, ""


def validate_paths(request: ScaffoldRequest) -> Tuple[bool, List[str]]:
    """Validate source/target paths. Returns (ok, errors)."""
    errors: List[str] = []

    # Planning repo must exist
    if not request.planning_repo.is_dir():
        errors.append(f"planning_repo does not exist: {request.planning_repo}")
        return False, errors

    # Source template must exist
    if not request.source_dir.is_dir():
        errors.append(f"source template directory does not exist: {request.source_dir}")
        return False, errors

    # Runtime repo must exist
    if not request.runtime_repo.is_dir():
        errors.append(f"runtime_repo does not exist: {request.runtime_repo}")
        return False, errors

    # apps/ directory must exist in runtime repo
    apps_dir = request.runtime_repo / "apps"
    if not apps_dir.is_dir():
        errors.append(f"apps directory does not exist: {apps_dir}")
        return False, errors

    # Path traversal check: target must be inside runtime_repo/apps/
    try:
        request.target_dir.resolve().relative_to(apps_dir.resolve())
    except ValueError:
        errors.append(
            f"target directory escapes apps/ scope: {request.target_dir}"
        )
        return False, errors

    # Source must be inside planning_repo/templates/
    try:
        request.source_dir.resolve().relative_to(
            (request.planning_repo / "templates").resolve()
        )
    except ValueError:
        errors.append(
            f"source directory escapes templates/ scope: {request.source_dir}"
        )
        return False, errors

    return True, []


def _should_exclude(path: Path) -> bool:
    """Check if a path should be excluded from copy (name-based, no filesystem check)."""
    name = path.name
    # Check directory name patterns
    if name in EXCLUDED_DIR_NAMES:
        return True
    # Check file name patterns
    for pattern in EXCLUDED_FILE_PATTERNS:
        if pattern.startswith("*."):
            ext = pattern[1:]  # e.g. ".pyc"
            if name.endswith(ext):
                return True
        elif name == pattern:
            return True
    return False


def _copy_template(source: Path, target: Path) -> List[str]:
    """
    Copy template directory to target, excluding build artifacts and caches.
    Returns list of copied file paths (relative to target).
    """
    copied: List[str] = []
    target.mkdir(parents=True, exist_ok=True)

    for item in source.rglob("*"):
        # Check each path component for exclusion
        skip = False
        for part in item.relative_to(source).parts:
            if part in EXCLUDED_DIR_NAMES:
                skip = True
                break
        if skip:
            continue

        # Check file patterns
        if item.is_file():
            name = item.name
            excluded = False
            for pattern in EXCLUDED_FILE_PATTERNS:
                if pattern.startswith("*."):
                    ext = pattern[1:]
                    if name.endswith(ext):
                        excluded = True
                        break
                elif name == pattern:
                    excluded = True
                    break
            if excluded:
                continue

        rel = item.relative_to(source)
        dest = target / rel

        if item.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dest)
            copied.append(str(rel))

    return sorted(copied)


def _generate_app_yaml(slug: str, profile: str) -> str:
    """Generate bounded app.yaml content."""
    # Database type mapping (must match select_profile.py derive_app_manifest)
    db_map = {
        "static-vite": "none",
        "react-hono-sqlite": "sqlite",
        "react-hono-postgres": "postgres",
        "next-postgres": "postgres",
        "python-fastapi": "sqlite",  # default; can be overridden by brief
        "python-service": "sqlite",  # default; can be overridden by brief
    }
    db_type = db_map.get(profile, "none")

    lines = [
        "# Bounded deployment manifest per templates/CONTRACTS.md (Phase-11)",
        "# Generated by scaffold_template.py from profile template",
        f"schema: 1",
        f"slug: {slug}",
        f"profile: {profile}",
        "routing:",
        "  mode: path",
        "database:",
        f"  type: {db_type}",
        "auth:",
        "  forward_auth: true",
        "  method: authentik-forward-auth",
        "  identity_headers:",
        "    - X-authentik-username",
        "    - X-authentik-email",
        "    - X-authentik-name",
        "    - X-authentik-uid",
        "    - X-authentik-groups",
        "",
    ]
    return "\n".join(lines)


def scaffold(request: ScaffoldRequest) -> dict:
    """
    Execute scaffolding. Returns result dict.
    Raises ValueError on validation failures.
    """
    # 1. Validate profile
    profile_ok, profile_err = validate_profile(request.profile)
    if not profile_ok:
        raise ValueError(profile_err)

    # 2. Validate slug
    slug_ok, slug_err = validate_slug(request.slug)
    if not slug_ok:
        raise ValueError(slug_err)

    # 3. Validate paths
    paths_ok, path_errors = validate_paths(request)
    if not paths_ok:
        raise ValueError("; ".join(path_errors))

    # 4. Check target existence
    if request.target_dir.exists() and any(request.target_dir.iterdir()):
        if not request.allow_overwrite:
            raise ValueError(
                f"target directory already exists and is not empty: {request.target_dir}. "
                "Use allow_overwrite=True to overwrite."
            )

    # 5. Copy template
    copied_files = _copy_template(request.source_dir, request.target_dir)

    # 6. Generate app.yaml
    app_yaml_content = _generate_app_yaml(request.slug, request.profile)
    app_yaml_path = request.target_dir / "app.yaml"
    app_yaml_path.write_text(app_yaml_content, encoding="utf-8")
    copied_files.append("app.yaml")
    copied_files.sort()

    return {
        "status": "SUCCESS",
        "profile": request.profile,
        "slug": request.slug,
        "source": str(request.source_dir),
        "target": str(request.target_dir),
        "files_copied": copied_files,
        "files_count": len(copied_files),
        "app_yaml_generated": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deterministic Template → App Scaffolding (Portable Core)"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # scaffold subcommand
    sc = subparsers.add_parser("scaffold", help="Scaffold an app from a profile template")
    sc.add_argument("--profile", required=True, help="Profile name (e.g., react-hono-sqlite)")
    sc.add_argument("--slug", required=True, help="App slug (e.g., shopping-list)")
    sc.add_argument(
        "--planning-repo",
        type=Path,
        required=True,
        help="Path to planning repo (contains templates/)",
    )
    sc.add_argument(
        "--runtime-repo",
        type=Path,
        required=True,
        help="Path to runtime repo (contains apps/)",
    )
    sc.add_argument(
        "--allow-overwrite",
        action="store_true",
        default=False,
        help="Allow overwriting existing app directory",
    )

    # validate-slug subcommand
    vs = subparsers.add_parser("validate-slug", help="Validate a slug format")
    vs.add_argument("slug", help="Slug to validate")

    # list-profiles subcommand
    subparsers.add_parser("list-profiles", help="List supported profiles")

    args = parser.parse_args()

    if args.command == "scaffold":
        request = ScaffoldRequest(
            profile=args.profile,
            slug=args.slug,
            planning_repo=args.planning_repo.resolve(),
            runtime_repo=args.runtime_repo.resolve(),
            allow_overwrite=args.allow_overwrite,
        )
        try:
            result = scaffold(request)
            print(json.dumps(result, indent=2))
            sys.exit(0)
        except ValueError as exc:
            print(json.dumps({"status": "FAILED", "error": str(exc)}, indent=2))
            sys.exit(1)

    elif args.command == "validate-slug":
        ok, err = validate_slug(args.slug)
        if ok:
            print(json.dumps({"valid": True, "slug": args.slug}))
            sys.exit(0)
        else:
            print(json.dumps({"valid": False, "slug": args.slug, "error": err}))
            sys.exit(1)

    elif args.command == "list-profiles":
        print(json.dumps({"profiles": list(SUPPORTED_PROFILES)}, indent=2))
        sys.exit(0)


if __name__ == "__main__":
    main()
