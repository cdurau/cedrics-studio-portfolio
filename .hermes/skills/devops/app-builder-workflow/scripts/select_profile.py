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
    def from_dict(cls, data: Dict[str, Any]) -> "AppRequirements":
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
    profile: Optional[str]
    is_escalated: bool
    escalation_reason: Optional[str]
    rationale: str
    dimensions: Dict[str, bool]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def validate_requirements_instance(req: AppRequirements) -> None:
    if req.shared_state and not req.backend_needed:
        raise ValueError(
            "Contradictory requirements: 'shared_state' is True but 'backend_needed' is False "
            "(shared/server-side state strictly requires a backend service)."
        )
    if req.python_needed and not req.backend_needed:
        raise ValueError(
            "Contradictory requirements: 'python_needed' is True but 'backend_needed' is False "
            "(Python libraries strictly require a backend service)."
        )


def validate_requirements_dict(data: Dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise TypeError(f"Requirements must be a dictionary or AppRequirements, got {type(data).__name__}")
    missing = [dim for dim in CANONICAL_DIMENSIONS if dim not in data]
    if missing:
        raise ValueError(f"Missing canonical requirement dimension(s): {', '.join(missing)}")
    for dim in CANONICAL_DIMENSIONS:
        val = data[dim]
        if not isinstance(val, bool):
            raise TypeError(f"Requirement '{dim}' must be a boolean, got {type(val).__name__} ({val!r})")
    if data["shared_state"] and not data["backend_needed"]:
        raise ValueError(
            "Contradictory requirements: 'shared_state' is True but 'backend_needed' is False "
            "(shared/server-side state strictly requires a backend service)."
        )
    if data["python_needed"] and not data["backend_needed"]:
        raise ValueError(
            "Contradictory requirements: 'python_needed' is True but 'backend_needed' is False "
            "(Python libraries strictly require a backend service)."
        )


def _get_field(requirements: Union[AppRequirements, Dict[str, Any], Any], key: str, default: Any = None) -> Any:
    if isinstance(requirements, dict):
        return requirements.get(key, default)
    return getattr(requirements, key, default)


def select_profile(requirements: Union[AppRequirements, Dict[str, Any], Any]) -> str:
    own_origin = _get_field(requirements, "own_origin_needed", False)
    path_safe = _get_field(requirements, "path_safe", True)
    forward_auth = _get_field(requirements, "forward_auth_sufficient", True)
    python_needed = _get_field(requirements, "python_needed", False)
    shared_state = _get_field(requirements, "shared_state", False)
    ssr_rsc_needed = _get_field(requirements, "ssr_rsc_needed", False)
    sqlite_sufficient = _get_field(requirements, "sqlite_sufficient", False)
    backend_needed = _get_field(requirements, "backend_needed", False)

    if own_origin:
        return "ESCALATE_TO_CEDRIC: own_origin_needed is true — subdomain routing not available in Phase 10/11"
    if not path_safe:
        return "ESCALATE_TO_CEDRIC: path_safe is false — only path-based routing is supported in Phase 10/11"
    if not forward_auth:
        return "ESCALATE_TO_CEDRIC: forward_auth_sufficient is false — frozen ForwardAuth is the only auth path in Phase 10/11"
    if ssr_rsc_needed:
        if python_needed:
            return "ESCALATE_TO_CEDRIC: Python + SSR/RSC combination has no canonical profile (catalog conflict)"
        return "ESCALATE_TO_CEDRIC: next-postgres requires subdomain routing — not available in Phase 10/11"
    if python_needed:
        return "python-fastapi" if shared_state else "python-service"
    if not shared_state:
        if backend_needed:
            return "ESCALATE_TO_CEDRIC: backend_needed=true but shared_state=false has no canonical profile (stateless backend)"
        return "static-vite"
    if sqlite_sufficient:
        return "react-hono-sqlite"
    return "react-hono-postgres"


def evaluate_requirements(requirements: Union[AppRequirements, Dict[str, Any]]) -> ProfileSelectionResult:
    if isinstance(requirements, dict):
        validate_requirements_dict(requirements)
        req_obj = AppRequirements.from_dict(requirements)
    elif isinstance(requirements, AppRequirements):
        validate_requirements_instance(requirements)
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

    if profile == "static-vite":
        db_type = "none"
    elif profile == "react-hono-sqlite":
        db_type = "sqlite"
    elif profile in ("react-hono-postgres", "next-postgres"):
        db_type = "postgres"
    elif profile in ("python-fastapi", "python-service"):
        db_type = "sqlite" if requirements_dict.get("sqlite_sufficient", True) else "postgres"
    else:
        db_type = "none"

    return {
        "schema": 1,
        "slug": slug,
        "profile": profile,
        "routing": {"mode": "path"},
        "database": {"type": db_type},
        "auth": {
            "forward_auth": True,
            "method": "authentik-forward-auth",
            "identity_headers": list(STANDARD_IDENTITY_HEADERS),
        },
    }


def validate_app_brief(brief_data: Dict[str, Any]) -> Tuple[bool, List[str]]:
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
            except Exception as exc:
                errors.append(f"Invalid requirements: {exc}")

    ps = brief_data.get("profile_selection")
    if isinstance(ps, dict) and isinstance(app, dict) and isinstance(app.get("requirements"), dict):
        try:
            computed = evaluate_requirements(app["requirements"])
            declared_profile = ps.get("profile")
            if computed.is_escalated and declared_profile is not None:
                errors.append(
                    f"profile_selection.profile should be null for escalated requirements, got '{declared_profile}'"
                )
            elif not computed.is_escalated and declared_profile != computed.profile:
                errors.append(
                    f"profile_selection.profile '{declared_profile}' does not match computed profile '{computed.profile}'"
                )
        except Exception as exc:
            errors.append(f"Cannot validate profile_selection consistency: {exc}")

    return len(errors) == 0, errors


def validate_app_brief_against_schema(brief_path: Path) -> Tuple[bool, List[str]]:
    schema_path = brief_path.parent / "app-brief.schema.json"
    if not schema_path.exists():
        return False, [f"Schema file not found: {schema_path}"]
    try:
        with open(brief_path, "r", encoding="utf-8") as f:
            brief_data = json.load(f)
        with open(schema_path, "r", encoding="utf-8") as f:
            schema_data = json.load(f)
    except Exception as exc:
        return False, [f"Failed to load JSON: {exc}"]

    try:
        import jsonschema
    except ImportError:
        return False, [
            "VALIDATION_DEPENDENCY_MISSING: jsonschema is required for canonical Draft 2020-12 validation"
        ]

    validator = jsonschema.Draft202012Validator(schema_data)
    validation_errors = sorted(validator.iter_errors(brief_data), key=lambda err: list(err.path))
    if validation_errors:
        return False, [f"Schema validation error: {err.message}" for err in validation_errors]
    return True, []


def validate_app_brief_file(brief_path: Path) -> Tuple[bool, List[str]]:
    """Fail-closed canonical validation: JSON Schema first, semantic checks second."""
    schema_ok, schema_errors = validate_app_brief_against_schema(brief_path)
    if not schema_ok:
        return False, schema_errors
    try:
        with open(brief_path, "r", encoding="utf-8") as f:
            brief_data = json.load(f)
    except Exception as exc:
        return False, [f"Failed to load JSON: {exc}"]
    semantic_ok, semantic_errors = validate_app_brief(brief_data)
    if not semantic_ok:
        return False, semantic_errors
    return True, []


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic Profile Selection CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    eval_parser = subparsers.add_parser("evaluate", help="Evaluate 9 dimensions from CLI flags")
    eval_parser.add_argument("--shared-state", action="store_true", default=False)
    eval_parser.add_argument("--python-needed", action="store_true", default=False)
    eval_parser.add_argument("--ssr-rsc-needed", action="store_true", default=False)
    eval_parser.add_argument("--sqlite-sufficient", action="store_true", default=False)
    eval_parser.add_argument("--path-safe", action="store_true", default=True)
    eval_parser.add_argument("--no-path-safe", action="store_false", dest="path_safe")
    eval_parser.add_argument("--own-origin-needed", action="store_true", default=False)
    eval_parser.add_argument("--forward-auth-sufficient", action="store_true", default=True)
    eval_parser.add_argument(
        "--no-forward-auth-sufficient",
        action="store_false",
        dest="forward_auth_sufficient",
        help="F3: ForwardAuth insufficient (escalation gate)",
    )
    eval_parser.add_argument("--shadcn-needed", action="store_true", default=False)
    eval_parser.add_argument("--backend-needed", action="store_true", default=False)
    eval_parser.add_argument("--no-backend-needed", action="store_false", dest="backend_needed")

    brief_parser = subparsers.add_parser("from-brief", help="Evaluate profile selection from app-brief.json")
    brief_parser.add_argument("brief_path", type=Path, help="Path to app-brief.json")

    derive_parser = subparsers.add_parser("derive-manifest", help="Derive app.yaml from app-brief.json")
    derive_parser.add_argument("brief_path", type=Path, help="Path to app-brief.json")
    derive_parser.add_argument("-o", "--output", type=Path, help="Optional output path for app.yaml")

    val_parser = subparsers.add_parser("validate", help="Validate app-brief.json against schema and semantics")
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
        res = evaluate_requirements(data["app"]["requirements"])
        print(json.dumps(res.to_dict(), indent=2))
    elif args.command == "derive-manifest":
        with open(args.brief_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        manifest_yaml = _dump_yaml_simple(derive_app_manifest(data))
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(manifest_yaml)
            print(f"Manifest written to {args.output}")
        else:
            print(manifest_yaml)
    elif args.command == "validate":
        ok, errors = validate_app_brief_file(args.brief_path)
        if ok:
            print("VALID: app-brief.json passes Draft 2020-12 schema and semantic validation.")
            sys.exit(0)
        print("INVALID: errors found:")
        for err in errors:
            print(f" - {err}")
        sys.exit(1)


def _dump_yaml_simple(data: Dict[str, Any]) -> str:
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
    for header in data["auth"]["identity_headers"]:
        lines.append(f"    - {header}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
