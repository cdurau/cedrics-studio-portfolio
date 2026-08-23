#!/usr/bin/env python3
"""Deterministic template-to-app scaffolding for the Portable App Builder Core.

The module is deliberately agent-neutral and stdlib-only. It copies exactly one
approved profile from ``<planning_repo>/templates/<profile>`` into
``<runtime_repo>/apps/<slug>`` and generates the bounded ``app.yaml`` manifest.
All validation is fail-closed and all writes remain inside the target app scope.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Set, Tuple

SUPPORTED_PROFILES: Tuple[str, ...] = (
    "static-vite",
    "react-hono-sqlite",
    "react-hono-postgres",
    "next-postgres",
    "python-fastapi",
    "python-service",
)

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,46}[a-z0-9]$")

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

EXCLUDED_FILE_PATTERNS: Tuple[str, ...] = (
    "*.pyc",
    "*.pyo",
    ".DS_Store",
    "Thumbs.db",
    "*.egg-info",
)

_INTERNAL_STAGE_NAME = ".scaffold-stage"
_INTERNAL_BACKUP_NAME = ".scaffold-backup"
_RESERVED_TARGET_NAMES = {_INTERNAL_STAGE_NAME, _INTERNAL_BACKUP_NAME}


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
    """Validate the portable app slug contract."""
    if not slug:
        return False, "slug is empty"
    if len(slug) < 2:
        return False, f"slug '{slug}' is too short (min 2 chars)"
    if len(slug) > 48:
        return False, f"slug '{slug}' is too long (max 48 chars)"
    if not SLUG_PATTERN.fullmatch(slug):
        return False, (
            f"slug '{slug}' is invalid — must be lowercase alphanumeric + hyphens, "
            "start and end with alnum, 2-48 chars"
        )
    if "--" in slug:
        return False, f"slug '{slug}' must not contain consecutive hyphens"
    return True, ""


def validate_profile(profile: str) -> Tuple[bool, str]:
    """Accept only one of the six canonical, explicitly approved profiles."""
    if profile not in SUPPORTED_PROFILES:
        return False, (
            f"profile '{profile}' is not supported. "
            f"Supported: {', '.join(SUPPORTED_PROFILES)}"
        )
    return True, ""


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_paths(request: ScaffoldRequest) -> Tuple[bool, List[str]]:
    """Validate repository and source/target boundaries without writing anything."""
    errors: List[str] = []

    if not request.planning_repo.is_dir():
        return False, [f"planning_repo does not exist: {request.planning_repo}"]
    if not request.runtime_repo.is_dir():
        return False, [f"runtime_repo does not exist: {request.runtime_repo}"]

    try:
        planning_root = request.planning_repo.resolve(strict=True)
        runtime_root = request.runtime_repo.resolve(strict=True)
    except OSError as exc:
        return False, [f"failed to resolve repository roots: {exc}"]

    if planning_root == runtime_root:
        return False, ["planning_repo and runtime_repo must be distinct repositories"]

    templates_dir = request.planning_repo / "templates"
    apps_dir = request.runtime_repo / "apps"
    if not templates_dir.is_dir():
        return False, [f"templates directory does not exist: {templates_dir}"]
    if not apps_dir.is_dir():
        return False, [f"apps directory does not exist: {apps_dir}"]
    if templates_dir.is_symlink():
        return False, [f"templates directory must not be a symlink: {templates_dir}"]
    if apps_dir.is_symlink():
        return False, [f"apps directory must not be a symlink: {apps_dir}"]
    if not request.source_dir.is_dir():
        return False, [f"source template directory does not exist: {request.source_dir}"]
    if request.source_dir.is_symlink():
        return False, [f"source template directory must not be a symlink: {request.source_dir}"]

    try:
        templates_root = templates_dir.resolve(strict=True)
        source_root = request.source_dir.resolve(strict=True)
        apps_root = apps_dir.resolve(strict=True)
        target_root = request.target_dir.resolve(strict=False)
    except OSError as exc:
        return False, [f"failed to resolve source/target paths: {exc}"]

    if not _is_relative_to(source_root, templates_root):
        errors.append(f"source directory escapes templates/ scope: {request.source_dir}")
    if not _is_relative_to(target_root, apps_root):
        errors.append(f"target directory escapes apps/ scope: {request.target_dir}")
    if request.target_dir.is_symlink():
        errors.append(f"target directory must not be a symlink: {request.target_dir}")

    return (not errors), errors


def _should_exclude(path: Path) -> bool:
    name = path.name
    if name in EXCLUDED_DIR_NAMES:
        return True
    for pattern in EXCLUDED_FILE_PATTERNS:
        if pattern.startswith("*.") and name.endswith(pattern[1:]):
            return True
        if name == pattern:
            return True
    return False


def _validate_template_tree(source: Path) -> None:
    """Reject symlinks/special files before any target mutation occurs."""
    source_root = source.resolve(strict=True)
    for root_text, dir_names, file_names in os.walk(source, topdown=True, followlinks=False):
        root = Path(root_text)
        dir_names.sort()
        file_names.sort()

        for name in list(dir_names):
            path = root / name
            if name in _RESERVED_TARGET_NAMES:
                raise ValueError(f"template contains reserved scaffold path: {path.relative_to(source)}")
            if path.is_symlink():
                raise ValueError(f"template contains symlink directory: {path.relative_to(source)}")
            try:
                mode = path.lstat().st_mode
            except OSError as exc:
                raise ValueError(f"cannot inspect template path {path}: {exc}") from exc
            if not stat.S_ISDIR(mode):
                raise ValueError(f"template contains non-directory entry in directory set: {path}")

        dir_names[:] = [name for name in dir_names if name not in EXCLUDED_DIR_NAMES]

        for name in file_names:
            path = root / name
            if name in _RESERVED_TARGET_NAMES:
                raise ValueError(f"template contains reserved scaffold path: {path.relative_to(source)}")
            if path.is_symlink():
                raise ValueError(f"template contains symlink file: {path.relative_to(source)}")
            try:
                mode = path.lstat().st_mode
            except OSError as exc:
                raise ValueError(f"cannot inspect template file {path}: {exc}") from exc
            if not stat.S_ISREG(mode):
                raise ValueError(f"template contains non-regular file: {path.relative_to(source)}")

        try:
            resolved_root = root.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"cannot resolve template directory {root}: {exc}") from exc
        if not _is_relative_to(resolved_root, source_root):
            raise ValueError(f"template traversal escapes source root: {root}")


def _copy_template(source: Path, target: Path) -> List[str]:
    """Copy one validated template deterministically to ``target``."""
    _validate_template_tree(source)
    copied: List[str] = []
    target.mkdir(parents=True, exist_ok=True)

    for root_text, dir_names, file_names in os.walk(source, topdown=True, followlinks=False):
        root = Path(root_text)
        dir_names[:] = sorted(name for name in dir_names if name not in EXCLUDED_DIR_NAMES)
        file_names.sort()
        rel_root = root.relative_to(source)

        for name in dir_names:
            (target / rel_root / name).mkdir(parents=True, exist_ok=True)

        for name in file_names:
            src = root / name
            if _should_exclude(src):
                continue
            rel = rel_root / name
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest, follow_symlinks=False)
            copied.append(rel.as_posix())

    return sorted(copied)


def _generate_app_yaml(slug: str, profile: str) -> str:
    """Generate the profile-baseline bounded manifest used by the existing Core contract."""
    db_map = {
        "static-vite": "none",
        "react-hono-sqlite": "sqlite",
        "react-hono-postgres": "postgres",
        "next-postgres": "postgres",
        "python-fastapi": "sqlite",
        "python-service": "sqlite",
    }
    db_type = db_map[profile]
    return "\n".join(
        [
            "# Bounded deployment manifest per templates/CONTRACTS.md (Phase-11)",
            "# Generated deterministically from the approved profile baseline",
            "schema: 1",
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
    )


def _promote_stage(target: Path, stage: Path, target_existed: bool) -> None:
    """Promote staged content while keeping rollback data inside the app scope."""
    backup = target / _INTERNAL_BACKUP_NAME
    if backup.exists() or backup.is_symlink():
        raise ValueError(f"reserved scaffold path already exists: {backup}")

    prior_entries = [entry for entry in target.iterdir() if entry != stage]
    promoted: List[Path] = []
    backup.mkdir()
    try:
        for entry in prior_entries:
            os.replace(entry, backup / entry.name)
        for entry in sorted(stage.iterdir(), key=lambda p: p.name):
            destination = target / entry.name
            os.replace(entry, destination)
            promoted.append(destination)
        stage.rmdir()
        shutil.rmtree(backup)
    except Exception:
        for entry in promoted:
            if entry.is_symlink() or entry.is_file():
                entry.unlink(missing_ok=True)
            elif entry.exists():
                shutil.rmtree(entry)
        if backup.exists():
            for entry in list(backup.iterdir()):
                os.replace(entry, target / entry.name)
            backup.rmdir()
        if stage.exists():
            shutil.rmtree(stage)
        if not target_existed and target.exists() and not any(target.iterdir()):
            target.rmdir()
        raise


def scaffold(request: ScaffoldRequest) -> dict:
    """Scaffold one app. Validation or write failures raise ``ValueError``/``OSError``."""
    profile_ok, profile_error = validate_profile(request.profile)
    if not profile_ok:
        raise ValueError(profile_error)
    slug_ok, slug_error = validate_slug(request.slug)
    if not slug_ok:
        raise ValueError(slug_error)
    paths_ok, path_errors = validate_paths(request)
    if not paths_ok:
        raise ValueError("; ".join(path_errors))

    _validate_template_tree(request.source_dir)

    target = request.target_dir
    target_existed = target.exists()
    if target_existed and not target.is_dir():
        raise ValueError(f"target exists but is not a directory: {target}")
    if target_existed and any(target.iterdir()) and not request.allow_overwrite:
        raise ValueError(
            f"target directory already exists and is not empty: {target}. "
            "Use allow_overwrite=True only under an explicit overwrite contract."
        )
    if target_existed:
        for reserved in _RESERVED_TARGET_NAMES:
            if (target / reserved).exists() or (target / reserved).is_symlink():
                raise ValueError(f"reserved scaffold path already exists: {target / reserved}")
    else:
        target.mkdir(parents=False)

    stage = target / _INTERNAL_STAGE_NAME
    try:
        stage.mkdir()
        copied_files = _copy_template(request.source_dir, stage)
        app_yaml_path = stage / "app.yaml"
        app_yaml_path.write_text(_generate_app_yaml(request.slug, request.profile), encoding="utf-8")
        copied_files = sorted(set(copied_files + ["app.yaml"]))
        _promote_stage(target, stage, target_existed)
    except Exception:
        if stage.exists():
            shutil.rmtree(stage)
        backup = target / _INTERNAL_BACKUP_NAME
        if backup.exists():
            for entry in list(backup.iterdir()):
                os.replace(entry, target / entry.name)
            backup.rmdir()
        if not target_existed and target.exists():
            shutil.rmtree(target)
        raise

    return {
        "status": "SUCCESS",
        "profile": request.profile,
        "slug": request.slug,
        "source": str(request.source_dir),
        "target": str(target),
        "files_copied": copied_files,
        "files_count": len(copied_files),
        "app_yaml_generated": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deterministic Template → App Scaffolding (Portable Core)"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scaffold_parser = subparsers.add_parser("scaffold", help="Scaffold an app from a profile template")
    scaffold_parser.add_argument("--profile", required=True)
    scaffold_parser.add_argument("--slug", required=True)
    scaffold_parser.add_argument("--planning-repo", type=Path, required=True)
    scaffold_parser.add_argument("--runtime-repo", type=Path, required=True)
    scaffold_parser.add_argument(
        "--allow-overwrite",
        action="store_true",
        default=False,
        help="Allow replacement of an existing non-empty app only under an explicit overwrite contract",
    )

    slug_parser = subparsers.add_parser("validate-slug", help="Validate a slug format")
    slug_parser.add_argument("slug")
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
            print(json.dumps(scaffold(request), indent=2))
            raise SystemExit(0)
        except (ValueError, OSError) as exc:
            print(json.dumps({"status": "FAILED", "error": str(exc)}, indent=2))
            raise SystemExit(1)

    if args.command == "validate-slug":
        ok, error = validate_slug(args.slug)
        payload = {"valid": ok, "slug": args.slug}
        if error:
            payload["error"] = error
        print(json.dumps(payload))
        raise SystemExit(0 if ok else 1)

    print(json.dumps({"profiles": list(SUPPORTED_PROFILES)}, indent=2))


if __name__ == "__main__":
    main()
