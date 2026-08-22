#!/usr/bin/env python3
"""
select_profile.py — Deterministic Profile Selection Logic for Phase 11 App Builder Workflow.

Implements the remediated decision tree from Phase 11 Plan §6 and templates/CONTRACTS.md.
Classifies the 9 canonical dimensions into:
- Eligibility Dimensions: shared_state, python_needed, ssr_rsc_needed, sqlite_sufficient
- Routing Dimensions: path_safe, own_origin_needed
- UI/Evidence Dimensions: forward_auth_sufficient, shadcn_needed, backend_needed
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

CANONICAL_DIMENSIONS = (
    "shared_state",
    "python_needed",
    "ssr_rsc_needed",
    "sqlite_sufficient",
    "path_safe",
    "own_origin_needed",
    "forward_auth_sufficient",
    "shadcn_needed",
    "backend_needed",
)

SUPPORTED_PROFILES = (
    "static-vite",
    "react-hono-sqlite",
    "react-hono-postgres",
    "next-postgres",
    "python-fastapi",
    "python-service",
)

STANDARD_IDENTITY_HEADERS = [
    "X-authentik-username",
    "X-authentik-email",
    "X-authentik-name",
    "X-authentik-uid",
    "X-authentik-groups",
]


@dataclass(frozen=True)
class AppRequirements:
    """Canonical 9-dimension requirements for profile selection."""

    shared_state: bool
    python_needed: bool
    ssr_rsc_needed: bool
    sqlite_sufficient: bool
    path_safe: bool
    own_origin_needed: bool
    forward_auth_sufficient: bool
    shadcn_needed: bool
    backend_needed: bool

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> AppRequirements:
        """Create AppRequirements from a dictionary with strict validation."""
        validate_requirements_dict(data)
        return cls(
            shared_state=bool(data["shared_state"]),
            python_needed=bool(data["python_needed"]),
            ssr_rsc_needed=bool(data["ssr_rsc_needed"]),
            sqlite_sufficient=bool(data["sqlite_sufficient"]),
            path_safe=bool(data["path_safe"]),
            own_origin_needed=bool(data["own_origin_needed"]),
            forward_auth_sufficient=bool(data["forward_auth_sufficient"]),
            shadcn_needed=bool(data["shadcn_needed"]),
            backend_needed=bool(data["backend_needed"]),
        )

    def to_dict(self) -> Dict[str, bool]:
        return asdict(self)


@dataclass(frozen=True)
class ProfileSelectionResult:
    """Structured result of profile selection evaluation."""

    profile: Optional[str]
    is_escalated: bool
    escalation_reason: Optional[str]
    rationale: str
    dimensions: Dict[str, bool]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def validate_requirements_dict(data: Dict[str, Any]) -> None:
    """Fail-closed validation of requirements dictionary against canonical dimensions."""
    if not isinstance(data, dict):
        raise TypeError(f"Requirements must be a dictionary or AppRequirements, got {type(data).__name__}")

    missing = [dim for dim in CANONICAL_DIMENSIONS if dim not in data]
    if missing:
        raise ValueError(f"Missing canonical requirement dimension(s): {', '.join(missing)}")

    for dim in CANONICAL_DIMENSIONS:
        val = data[dim]
        if not isinstance(val, bool):
            raise TypeError(f"Requirement '{dim}' must be a boolean, got {type(val).__name__} ({val!r})")

    # Contradiction check: shared_state requires a backend
    if data["shared_state"] and not data["backend_needed"]:
        raise ValueError(
            "Contradictory requirements: 'shared_state' is True but 'backend_needed' is False "
            "(shared/server-side state strictly requires a backend service)."
        )

    # Contradiction check: python_needed requires a backend
    if data["python_needed"] and not data["backend_needed"]:
        raise ValueError(
            "Contradictory requirements: 'python_needed' is True but 'backend_needed' is False "
            "(Python libraries strictly require a backend service)."
        )


def _get_field(requirements: Union[AppRequirements, Dict[str, Any], Any], key: str, default: Any = None) -> Any:
    """Helper to access requirement field on both objects and dictionaries."""
    if isinstance(requirements, dict):
        return requirements.get(key, default)
    return getattr(requirements, key, default)


def select_profile(requirements: Union[AppRequirements, Dict[str, Any], Any]) -> str:
    """
    Deterministic Profile Selection Algorithm (Remediated Phase 11 / P11-M1).

    Order of evaluation:
    1. Routing escalations (own_origin_needed, path_safe)
    2. Python libraries (python_needed checked BEFORE shared_state)
    3. Shared/server-side state (shared_state)
    4. SSR/RSC (ssr_rsc_needed -> escalate next-postgres)
    5. SQLite vs PostgreSQL (sqlite_sufficient)
    6. Default: react-hono-postgres

    Returns:
        Profile string name (e.g. 'react-hono-sqlite') or escalation message starting with 'ESCALATE_TO_CEDRIC:'
    """
    # Helper getters supporting dicts and objects
    own_origin = _get_field(requirements, "own_origin_needed", False)
    path_safe = _get_field(requirements, "path_safe", True)
    python_needed = _get_field(requirements, "python_needed", False)
    shared_state = _get_field(requirements, "shared_state", False)
    ssr_rsc_needed = _get_field(requirements, "ssr_rsc_needed", False)
    sqlite_sufficient = _get_field(requirements, "sqlite_sufficient", False)

    # Escalation Leaf 1: own_origin_needed (no subdomain support in Phase 10/11)
    if own_origin:
        return "ESCALATE_TO_CEDRIC: own_origin_needed is true — subdomain routing not available in Phase 10/11"

    # Escalation Leaf 2: path_safe (only path-based routing is supported)
    if not path_safe:
        return "ESCALATE_TO_CEDRIC: path_safe is false — only path-based routing is supported in Phase 10/11"

    # Dimension 2: Python-specific libraries? (BEFORE shared_state!)
    if python_needed:
        if shared_state:
            return "python-fastapi"
        else:
            return "python-service"

    # Dimension 1: Shared/Server-side state?
    if not shared_state:
        return "static-vite"

    # Dimension 3: SSR/RSC needed?
    if ssr_rsc_needed:
        return "ESCALATE_TO_CEDRIC: next-postgres requires subdomain routing — not available in Phase 10/11"

    # Dimension 4: SQLite sufficient?
    if sqlite_sufficient:
        return "react-hono-sqlite"

    # Default: react-hono-postgres
    return "react-hono-postgres"


def evaluate_requirements(requirements: Union[AppRequirements, Dict[str, Any]]) -> ProfileSelectionResult:
    """
    Full evaluation of requirements with rationale and escalation metadata.
    """
    if isinstance(requirements, dict):
        validate_requirements_dict(requirements)
        req_obj = AppRequirements.from_dict(requirements)
    elif isinstance(requirements, AppRequirements):
        req_obj = requirements
    else:
        raise TypeError(f"Expected dict or AppRequirements, got {type(requirements).__name__}")

    raw_result = select_profile(req_obj)
    is_escalated = raw_result.startswith("ESCALATE_TO_CEDRIC:")

    if is_escalated:
        escalation_reason = raw_result
        profile = None
        rationale = f"Escalation required: {raw_result.replace('ESCALATE_TO_CEDRIC: ', '')}"
    else:
        escalation_reason = None
        profile = raw_result
        rationale = _build_rationale(req_obj, profile)

    return ProfileSelectionResult(
        profile=profile,
        is_escalated=is_escalated,
        escalation_reason=escalation_reason,
        rationale=rationale,
        dimensions=req_obj.to_dict(),
    )


def _build_rationale(req: AppRequirements, profile: str) -> str:
    """Generate human-readable rationale matching the chosen profile and requirements."""
    if profile == "static-vite":
        return "No shared/server-side state needed and no Python libraries required; client-only static PWA selected."
    if profile == "python-fastapi":
        return "Python-specific libraries required with shared state and UI; FastAPI selected."
    if profile == "python-service":
        return "Python-specific libraries required for headless service/API without shared UI state; python-service selected."
    if profile == "react-hono-sqlite":
        return (
            "Shared state needed, no Python, no SSR, SQLite sufficient for low concurrency, "
            "path-safe, no origin isolation needed, ForwardAuth sufficient."
        )
    if profile == "react-hono-postgres":
        return (
            "Shared state needed, no Python, no SSR, PostgreSQL justified for relational multi-user data, "
            "path-safe, no origin isolation needed, ForwardAuth sufficient."
        )
    return f"Profile '{profile}' selected based on evaluated requirements."


def derive_app_manifest(app_brief_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deterministically derive bounded app.yaml manifest from app-brief.json.
    Enforces One-Way Derivation principle.
    """
    if not isinstance(app_brief_data, dict):
        raise TypeError("app_brief_data must be a dictionary")

    app_info = app_brief_data.get("app", {})
    slug = app_info.get("slug")
    if not slug:
        raise ValueError("app-brief.json is missing required 'app.slug'")

    requirements_dict = app_info.get("requirements", {})
    result = evaluate_requirements(requirements_dict)

    if result.is_escalated:
        raise ValueError(f"Cannot derive app.yaml for escalated requirements: {result.escalation_reason}")

    profile = result.profile
    if not profile:
        raise ValueError("Could not determine profile for manifest derivation")

    # Determine database type
    if profile in ("static-vite",):
        db_type = "none"
    elif profile in ("react-hono-sqlite",):
        db_type = "sqlite"
    elif profile in ("react-hono-postgres", "next-postgres"):
        db_type = "postgres"
    elif profile in ("python-fastapi", "python-service"):
        db_type = "sqlite" if requirements_dict.get("sqlite_sufficient", True) else "postgres"
    else:
        db_type = "none"

    manifest: Dict[str, Any] = {
        "schema": 1,
        "slug": slug,
        "profile": profile,
        "routing": {
            "mode": "path",
        },
        "database": {
            "type": db_type,
        },
        "auth": {
            "forward_auth": True,
            "method": "authentik-forward-auth",
            "identity_headers": list(STANDARD_IDENTITY_HEADERS),
        },
    }

    return manifest


def validate_app_brief(brief_data: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Validate app-brief.json structure, required fields, and dimension consistency."""
    errors: List[str] = []

    if not isinstance(brief_data, dict):
        return False, ["App brief root must be a JSON object"]

    if brief_data.get("schema") != 1:
        errors.append(f"Expected schema version 1, got {brief_data.get('schema')!r}")

    app = brief_data.get("app")
    if not isinstance(app, dict):
        errors.append("Missing or invalid 'app' section")
    else:
        for field in ("name", "slug", "purpose"):
            if not app.get(field):
                errors.append(f"Missing required 'app.{field}'")

        reqs = app.get("requirements")
        if not isinstance(reqs, dict):
            errors.append("Missing or invalid 'app.requirements'")
        else:
            try:
                validate_requirements_dict(reqs)
            except Exception as e:
                errors.append(f"Invalid requirements: {e}")

    return len(errors) == 0, errors


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic Profile Selection CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: evaluate
    eval_parser = subparsers.add_parser("evaluate", help="Evaluate 9 dimensions from CLI flags")
    eval_parser.add_argument("--shared-state", action="store_true", default=False)
    eval_parser.add_argument("--python-needed", action="store_true", default=False)
    eval_parser.add_argument("--ssr-rsc-needed", action="store_true", default=False)
    eval_parser.add_argument("--sqlite-sufficient", action="store_true", default=False)
    eval_parser.add_argument("--path-safe", action="store_true", default=True)
    eval_parser.add_argument("--no-path-safe", action="store_false", dest="path_safe")
    eval_parser.add_argument("--own-origin-needed", action="store_true", default=False)
    eval_parser.add_argument("--forward-auth-sufficient", action="store_true", default=True)
    eval_parser.add_argument("--shadcn-needed", action="store_true", default=False)
    eval_parser.add_argument("--backend-needed", action="store_true", default=False)

    # Subcommand: from-brief
    brief_parser = subparsers.add_parser("from-brief", help="Evaluate profile selection from app-brief.json")
    brief_parser.add_argument("brief_path", type=Path, help="Path to app-brief.json")

    # Subcommand: derive-manifest
    derive_parser = subparsers.add_parser("derive-manifest", help="Derive app.yaml from app-brief.json")
    derive_parser.add_argument("brief_path", type=Path, help="Path to app-brief.json")
    derive_parser.add_argument("-o", "--output", type=Path, help="Optional output path for app.yaml")

    # Subcommand: validate
    val_parser = subparsers.add_parser("validate", help="Validate app-brief.json")
    val_parser.add_argument("brief_path", type=Path, help="Path to app-brief.json")

    args = parser.parse_args()

    if args.command == "evaluate":
        reqs = AppRequirements(
            shared_state=args.shared_state,
            python_needed=args.python_needed,
            ssr_rsc_needed=args.ssr_rsc_needed,
            sqlite_sufficient=args.sqlite_sufficient,
            path_safe=args.path_safe,
            own_origin_needed=args.own_origin_needed,
            forward_auth_sufficient=args.forward_auth_sufficient,
            shadcn_needed=args.shadcn_needed,
            backend_needed=args.backend_needed,
        )
        res = evaluate_requirements(reqs)
        print(json.dumps(res.to_dict(), indent=2))

    elif args.command == "from-brief":
        with open(args.brief_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        reqs = data["app"]["requirements"]
        res = evaluate_requirements(reqs)
        print(json.dumps(res.to_dict(), indent=2))

    elif args.command == "derive-manifest":
        with open(args.brief_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        manifest = derive_app_manifest(data)
        manifest_yaml = _dump_yaml_simple(manifest)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(manifest_yaml)
            print(f"Manifest written to {args.output}")
        else:
            print(manifest_yaml)

    elif args.command == "validate":
        with open(args.brief_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        ok, errors = validate_app_brief(data)
        if ok:
            print("VALID: app-brief.json is valid.")
            sys.exit(0)
        else:
            print("INVALID: errors found:")
            for err in errors:
                print(f" - {err}")
            sys.exit(1)


def _dump_yaml_simple(data: Dict[str, Any]) -> str:
    """Dump dictionary to clean YAML format without requiring third-party library."""
    lines: List[str] = [
        "# Bounded deployment manifest per templates/CONTRACTS.md (Phase-11)",
        "# Derived deterministically from app-brief.json",
        f"schema: {data['schema']}",
        f"slug: {data['slug']}",
        f"profile: {data['profile']}",
        "routing:",
        f"  mode: {data['routing']['mode']}",
        "database:",
        f"  type: {data['database']['type']}",
        "auth:",
        f"  forward_auth: {str(data['auth']['forward_auth']).lower()}",
        f"  method: {data['auth']['method']}",
        "  identity_headers:",
    ]
    for h in data["auth"]["identity_headers"]:
        lines.append(f"    - {h}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
