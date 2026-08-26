#!/usr/bin/env python3
"""
post_deploy_acceptance.py — Profile-specific Post-Deployment Acceptance Tests.

Agent-neutral, stdlib-only Python for Portable App Builder Core.

This module evaluates machine-verifiable post-deploy evidence. It does not
perform live HTTP, Docker, or Coolify calls. Callers (or tests) supply
already-collected check payloads. Missing or unclear evidence fails closed.

Executor self-reports are not accepted as evidence: every check requires an
explicit payload, and overall PASS is issued only when every required check
passes.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


SUPPORTED_PROFILES: Tuple[str, ...] = (
    "static-vite",
    "react-hono-sqlite",
    "react-hono-postgres",
    "next-postgres",
    "python-fastapi",
    "python-service",
)

PATH_PROFILES: Tuple[str, ...] = (
    "static-vite",
    "react-hono-sqlite",
    "react-hono-postgres",
    "python-fastapi",
)

SUBDOMAIN_PROFILES: Tuple[str, ...] = (
    "next-postgres",
    "python-service",
)

IDENTITY_HEADERS: Tuple[str, ...] = (
    "X-authentik-username",
    "X-authentik-email",
    "X-authentik-name",
    "X-authentik-uid",
    "X-authentik-groups",
)

COMMON_CHECKS: Tuple[str, ...] = (
    "resource_discovery",
    "github_actions_success",
    "expected_workflows_only",
    "coolify_deployment_success",
    "container_running_healthy",
    "healthz_ok",
    "authentik_redirect_unauthenticated",
    "authenticated_access",
    "strip_prefix_path_routing",
    "assets_load",
)

PROFILE_SPECIFIC_CHECKS: Dict[str, Tuple[str, ...]] = {
    "static-vite": (
        "healthz_ipv4",
        "healthz_ipv6",
        "spa_fallback",
        "no_backend_api",
    ),
    "react-hono-sqlite": (
        "sqlite_connection",
        "migration_success",
        "persistence_across_redeploy",
        "identity_headers",
    ),
    "react-hono-postgres": (
        "postgres_connection",
        "migration_success",
        "persistence_across_redeploy",
        "identity_headers",
    ),
    "next-postgres": (
        "ssr_rsc_render",
        "root_url_cookie_redirect",
        "postgres_connection",
        "identity_headers",
    ),
    "python-fastapi": (
        "api_endpoints",
        "db_connection",
        "migration_success",
        "persistence_across_redeploy",
    ),
    "python-service": (
        "api_endpoints",
        "db_connection",
        "migration_success",
        "persistence_across_redeploy",
    ),
}


class AcceptanceStatus(str, Enum):
    """Outcome of a single acceptance check or the overall result."""

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class AcceptanceCheck:
    """A single post-deployment acceptance check."""

    name: str
    status: AcceptanceStatus
    details: Optional[str] = None
    profile: Optional[str] = None

    def to_dict(self) -> dict:
        result: Dict[str, Any] = {
            "name": self.name,
            "status": self.status.value,
        }
        if self.details is not None:
            result["details"] = self.details
        if self.profile is not None:
            result["profile"] = self.profile
        return result


@dataclass(frozen=True)
class AcceptanceResult:
    """Complete post-deployment acceptance result for one profile."""

    profile: str
    checks: List[AcceptanceCheck]
    overall_status: AcceptanceStatus

    def to_dict(self) -> dict:
        return {
            "profile": self.profile,
            "checks": [check.to_dict() for check in self.checks],
            "overall_status": self.overall_status.value,
        }

    def failed_checks(self) -> List[AcceptanceCheck]:
        return [check for check in self.checks if check.status is AcceptanceStatus.FAIL]


def get_profile_specific_checks(profile: str) -> List[str]:
    """Return the ordered list of profile-specific check names.

    Unknown profiles return an empty list. Callers must still fail closed
    via ``run_acceptance`` — this helper is informational only.
    """
    return list(PROFILE_SPECIFIC_CHECKS.get(profile, ()))


def get_common_checks() -> List[str]:
    """Return the checks that apply to every supported profile."""
    return list(COMMON_CHECKS)


def get_all_required_checks(profile: str) -> List[str]:
    """Return common + profile-specific check names for a profile."""
    return get_common_checks() + get_profile_specific_checks(profile)


def infer_routing_mode(profile: str, routing_mode: Optional[str] = None) -> str:
    """Infer routing mode. Explicit value wins; otherwise profile default."""
    if routing_mode:
        normalized = routing_mode.strip().lower()
        if normalized in {"path", "subdomain", "service"}:
            return normalized
        return "unknown"
    if profile in SUBDOMAIN_PROFILES:
        return "subdomain"
    return "path"


def _as_mapping(value: Any) -> Optional[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return value
    return None


def _parse_json_object(value: Any) -> Optional[Any]:
    if isinstance(value, (dict, list, bool, int, float)) or value is None:
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def evaluate_healthz_payload(health_check: Optional[Mapping[str, Any]], *, label: str = "healthz") -> AcceptanceCheck:
    """Evaluate a /healthz payload. Requires HTTP 200 and body ``{"ok": true}``."""
    if health_check is None:
        return AcceptanceCheck(
            name=label,
            status=AcceptanceStatus.FAIL,
            details="missing health check evidence (fail-closed)",
        )

    status_code = health_check.get("status_code", health_check.get("status"))
    if status_code != 200:
        return AcceptanceCheck(
            name=label,
            status=AcceptanceStatus.FAIL,
            details=f"health check status is {status_code!r}, expected 200",
        )

    body = health_check.get("body", health_check.get("json"))
    parsed = _parse_json_object(body)
    if not isinstance(parsed, Mapping):
        return AcceptanceCheck(
            name=label,
            status=AcceptanceStatus.FAIL,
            details="health check body is not a JSON object",
        )

    if parsed.get("ok") is not True:
        return AcceptanceCheck(
            name=label,
            status=AcceptanceStatus.FAIL,
            details=f"health check body ok is {parsed.get('ok')!r}, expected true",
        )

    return AcceptanceCheck(
        name=label,
        status=AcceptanceStatus.PASS,
        details="healthz returned {\"ok\":true} HTTP 200",
    )


def _check_or_missing(
    name: str,
    payload: Optional[Mapping[str, Any]],
    predicate_ok: bool,
    pass_details: str,
    fail_details: str,
    missing_details: Optional[str] = None,
    profile: Optional[str] = None,
) -> AcceptanceCheck:
    if payload is None:
        return AcceptanceCheck(
            name=name,
            status=AcceptanceStatus.FAIL,
            details=missing_details or f"missing {name} evidence (fail-closed)",
            profile=profile,
        )
    if predicate_ok:
        return AcceptanceCheck(
            name=name,
            status=AcceptanceStatus.PASS,
            details=pass_details,
            profile=profile,
        )
    return AcceptanceCheck(
        name=name,
        status=AcceptanceStatus.FAIL,
        details=fail_details,
        profile=profile,
    )


def _evaluate_discovery(discovery_result: Optional[Mapping[str, Any]]) -> AcceptanceCheck:
    if discovery_result is None:
        return AcceptanceCheck(
            name="resource_discovery",
            status=AcceptanceStatus.FAIL,
            details="missing discovery result (fail-closed)",
        )
    success = discovery_result.get("success")
    container_name = discovery_result.get("container_name")
    if success is not True:
        return AcceptanceCheck(
            name="resource_discovery",
            status=AcceptanceStatus.FAIL,
            details=f"discovery failed: {discovery_result.get('error') or 'unsuccessful result'}",
        )
    if not isinstance(container_name, str) or not container_name.strip():
        return AcceptanceCheck(
            name="resource_discovery",
            status=AcceptanceStatus.FAIL,
            details="discovery succeeded without a container name (fail-closed)",
        )
    return AcceptanceCheck(
        name="resource_discovery",
        status=AcceptanceStatus.PASS,
        details=f"discovered container {container_name}",
    )


def _evaluate_github_actions(
    github_actions: Optional[Mapping[str, Any]],
) -> Tuple[AcceptanceCheck, AcceptanceCheck]:
    if github_actions is None:
        missing = "missing GitHub Actions evidence (fail-closed)"
        return (
            AcceptanceCheck("github_actions_success", AcceptanceStatus.FAIL, missing),
            AcceptanceCheck("expected_workflows_only", AcceptanceStatus.FAIL, missing),
        )

    success = github_actions.get("success") is True or github_actions.get("conclusion") == "success"
    unexpected = github_actions.get("unexpected_workflows") or []
    if not isinstance(unexpected, list):
        unexpected = ["<invalid unexpected_workflows>"]

    success_check = _check_or_missing(
        "github_actions_success",
        github_actions,
        success,
        "GitHub Actions succeeded",
        "GitHub Actions did not succeed",
    )
    workflow_check = _check_or_missing(
        "expected_workflows_only",
        github_actions,
        len(unexpected) == 0,
        "only expected app workflows were triggered",
        f"unexpected workflows triggered: {unexpected}",
    )
    return success_check, workflow_check


def _evaluate_coolify(coolify_deployment: Optional[Mapping[str, Any]]) -> AcceptanceCheck:
    if coolify_deployment is None:
        return AcceptanceCheck(
            name="coolify_deployment_success",
            status=AcceptanceStatus.FAIL,
            details="missing Coolify deployment evidence (fail-closed)",
        )
    status = str(coolify_deployment.get("status") or "").strip().lower()
    success = coolify_deployment.get("success") is True or status in {"finished", "success", "healthy"}
    return _check_or_missing(
        "coolify_deployment_success",
        coolify_deployment,
        success,
        "Coolify deployment succeeded",
        f"Coolify deployment status is {coolify_deployment.get('status')!r}",
    )


def _evaluate_container(container: Optional[Mapping[str, Any]]) -> AcceptanceCheck:
    if container is None:
        return AcceptanceCheck(
            name="container_running_healthy",
            status=AcceptanceStatus.FAIL,
            details="missing container evidence (fail-closed)",
        )
    combined = str(container.get("status") or "").strip().lower()
    state = str(container.get("state") or "").strip().lower()
    health = str(container.get("health") or "").strip().lower()
    running_healthy = combined == "running:healthy" or (state == "running" and health == "healthy")
    return _check_or_missing(
        "container_running_healthy",
        container,
        running_healthy,
        "container is running:healthy",
        f"container is not running:healthy (state={state!r}, health={health!r}, status={combined!r})",
    )


def _evaluate_auth_redirect(auth_check: Optional[Mapping[str, Any]]) -> AcceptanceCheck:
    if auth_check is None:
        return AcceptanceCheck(
            name="authentik_redirect_unauthenticated",
            status=AcceptanceStatus.FAIL,
            details="missing unauthenticated auth evidence (fail-closed)",
        )
    status_code = auth_check.get("status_code", auth_check.get("status"))
    return _check_or_missing(
        "authentik_redirect_unauthenticated",
        auth_check,
        status_code == 302,
        "unauthenticated request received Authentik 302 redirect",
        f"unauthenticated status is {status_code!r}, expected 302",
    )


def _evaluate_authenticated_access(authenticated_access: Optional[Mapping[str, Any]]) -> AcceptanceCheck:
    if authenticated_access is None:
        return AcceptanceCheck(
            name="authenticated_access",
            status=AcceptanceStatus.FAIL,
            details="missing authenticated access evidence (fail-closed)",
        )
    status_code = authenticated_access.get("status_code", authenticated_access.get("status"))
    reachable = authenticated_access.get("reachable") is True or status_code == 200
    return _check_or_missing(
        "authenticated_access",
        authenticated_access,
        reachable,
        "authenticated access succeeded",
        "authenticated access did not succeed",
    )


def _evaluate_routing(
    profile: str,
    routing_check: Optional[Mapping[str, Any]],
    routing_mode: str,
) -> AcceptanceCheck:
    if routing_mode in {"subdomain", "service"} and routing_check is None:
        return AcceptanceCheck(
            name="strip_prefix_path_routing",
            status=AcceptanceStatus.SKIP,
            details=f"StripPrefix not required for {routing_mode} routing",
            profile=profile,
        )
    if routing_check is None:
        return AcceptanceCheck(
            name="strip_prefix_path_routing",
            status=AcceptanceStatus.FAIL,
            details="missing routing evidence (fail-closed)",
            profile=profile,
        )
    if routing_mode == "unknown":
        return AcceptanceCheck(
            name="strip_prefix_path_routing",
            status=AcceptanceStatus.FAIL,
            details="routing mode is unclear (fail-closed)",
            profile=profile,
        )
    correct = (
        routing_check.get("correct") is True
        or routing_check.get("strip_prefix") is True
        or (
            routing_check.get("app_sees_root") is True
            and bool(routing_check.get("public_prefix"))
        )
    )
    return _check_or_missing(
        "strip_prefix_path_routing",
        routing_check,
        correct,
        "StripPrefix/path routing is correct",
        "StripPrefix/path routing is incorrect",
        profile=profile,
    )


def _evaluate_assets(assets: Optional[Mapping[str, Any]]) -> AcceptanceCheck:
    if assets is None:
        return AcceptanceCheck(
            name="assets_load",
            status=AcceptanceStatus.FAIL,
            details="missing asset evidence (fail-closed)",
        )
    failed = assets.get("failed_assets") or []
    loaded = assets.get("loaded") is True or (isinstance(failed, list) and len(failed) == 0 and "failed_assets" in assets)
    return _check_or_missing(
        "assets_load",
        assets,
        loaded,
        "assets loaded correctly",
        f"assets failed to load: {failed}",
    )


def _flag(payload: Optional[Mapping[str, Any]], *keys: str) -> bool:
    if payload is None:
        return False
    for key in keys:
        if payload.get(key) is True:
            return True
    return False


def _evaluate_identity_headers(
    profile: str,
    payload: Optional[Mapping[str, Any]],
) -> AcceptanceCheck:
    if payload is None:
        return AcceptanceCheck(
            name="identity_headers",
            status=AcceptanceStatus.FAIL,
            details="missing identity header evidence (fail-closed)",
            profile=profile,
        )
    present = payload.get("headers_present") or payload.get("headers") or []
    if not isinstance(present, list):
        present = []
    present_normalized = {str(item) for item in present}
    required_found = all(header in present_normalized for header in IDENTITY_HEADERS)
    consumed = payload.get("consumed") is True
    ok = consumed and (required_found or payload.get("all_required_present") is True)
    return _check_or_missing(
        "identity_headers",
        payload,
        ok,
        "identity headers consumed",
        "identity headers missing or not consumed",
        profile=profile,
    )


def _evaluate_persistence(
    profile: str,
    payload: Optional[Mapping[str, Any]],
    name: str = "persistence_across_redeploy",
) -> AcceptanceCheck:
    if payload is None:
        return AcceptanceCheck(
            name=name,
            status=AcceptanceStatus.FAIL,
            details="missing persistence evidence (fail-closed)",
            profile=profile,
        )
    write_ok = payload.get("write_ok") is True or payload.get("write") is True
    read_ok = (
        payload.get("read_after_redeploy") is True
        or payload.get("read_ok") is True
        or payload.get("persisted") is True
    )
    return _check_or_missing(
        name,
        payload,
        write_ok and read_ok,
        "write survived redeploy/readback",
        "persistence test failed (write/read-after-redeploy)",
        profile=profile,
    )


def _evaluate_migration(profile: str, payload: Optional[Mapping[str, Any]]) -> AcceptanceCheck:
    if payload is None:
        return AcceptanceCheck(
            name="migration_success",
            status=AcceptanceStatus.FAIL,
            details="missing migration evidence (fail-closed)",
            profile=profile,
        )
    failed = payload.get("failed") is True
    applied = payload.get("applied") is True or payload.get("success") is True
    return _check_or_missing(
        "migration_success",
        payload,
        applied and not failed,
        "migrations applied successfully",
        "migration did not succeed",
        profile=profile,
    )


def _evaluate_connection(
    name: str,
    profile: str,
    payload: Optional[Mapping[str, Any]],
) -> AcceptanceCheck:
    if payload is None:
        return AcceptanceCheck(
            name=name,
            status=AcceptanceStatus.FAIL,
            details=f"missing {name} evidence (fail-closed)",
            profile=profile,
        )
    connected = (
        payload.get("connected") is True
        or payload.get("success") is True
        or payload.get("ok") is True
    )
    return _check_or_missing(
        name,
        payload,
        connected,
        f"{name} succeeded",
        f"{name} failed",
        profile=profile,
    )


def _evaluate_api_endpoints(profile: str, payload: Optional[Mapping[str, Any]]) -> AcceptanceCheck:
    if payload is None:
        return AcceptanceCheck(
            name="api_endpoints",
            status=AcceptanceStatus.FAIL,
            details="missing API endpoint evidence (fail-closed)",
            profile=profile,
        )
    if payload.get("reachable") is True:
        return AcceptanceCheck(
            name="api_endpoints",
            status=AcceptanceStatus.PASS,
            details="API endpoints reachable",
            profile=profile,
        )
    endpoints = payload.get("endpoints")
    if isinstance(endpoints, list) and endpoints:
        all_ok = True
        for item in endpoints:
            if not isinstance(item, Mapping):
                all_ok = False
                break
            status_code = item.get("status_code", item.get("status"))
            if status_code != 200 and item.get("ok") is not True:
                all_ok = False
                break
        if all_ok:
            return AcceptanceCheck(
                name="api_endpoints",
                status=AcceptanceStatus.PASS,
                details=f"{len(endpoints)} API endpoints reachable",
                profile=profile,
            )
    return AcceptanceCheck(
        name="api_endpoints",
        status=AcceptanceStatus.FAIL,
        details="API endpoints were not verified as reachable",
        profile=profile,
    )


def _evaluate_boolean_check(
    name: str,
    profile: str,
    payload: Optional[Mapping[str, Any]],
    *keys: str,
    pass_details: str,
    fail_details: str,
) -> AcceptanceCheck:
    if payload is None:
        return AcceptanceCheck(
            name=name,
            status=AcceptanceStatus.FAIL,
            details=f"missing {name} evidence (fail-closed)",
            profile=profile,
        )
    return _check_or_missing(
        name,
        payload,
        _flag(payload, *keys),
        pass_details,
        fail_details,
        profile=profile,
    )


def _evaluate_no_backend_api(profile: str, payload: Optional[Mapping[str, Any]]) -> AcceptanceCheck:
    if payload is None:
        return AcceptanceCheck(
            name="no_backend_api",
            status=AcceptanceStatus.FAIL,
            details="missing no-backend-api evidence (fail-closed)",
            profile=profile,
        )
    if payload.get("no_backend") is True:
        ok = True
    elif payload.get("backend_requests") == 0:
        ok = True
    elif payload.get("api_status_code") == 404:
        ok = True
    else:
        ok = False
    return _check_or_missing(
        "no_backend_api",
        payload,
        ok,
        "no backend API is exposed",
        "backend API evidence is missing or indicates an API exists",
        profile=profile,
    )


def _evaluate_root_url_cookie_redirect(
    profile: str,
    payload: Optional[Mapping[str, Any]],
) -> AcceptanceCheck:
    if payload is None:
        return AcceptanceCheck(
            name="root_url_cookie_redirect",
            status=AcceptanceStatus.FAIL,
            details="missing root URL/cookie/redirect evidence (fail-closed)",
            profile=profile,
        )
    ok = (
        payload.get("root_url_ok") is True
        and payload.get("cookie_ok") is True
        and payload.get("redirect_ok") is True
    )
    return _check_or_missing(
        "root_url_cookie_redirect",
        payload,
        ok,
        "root URL, cookie, and redirect are correct",
        "root URL, cookie, or redirect check failed",
        profile=profile,
    )


def _profile_payload(profile_checks: Optional[Mapping[str, Any]], *names: str) -> Optional[Mapping[str, Any]]:
    if profile_checks is None:
        return None
    for name in names:
        candidate = profile_checks.get(name)
        mapped = _as_mapping(candidate)
        if mapped is not None:
            return mapped
    return None


def _evaluate_profile_specific(
    profile: str,
    health_check: Optional[Mapping[str, Any]],
    profile_checks: Optional[Mapping[str, Any]],
) -> List[AcceptanceCheck]:
    checks: List[AcceptanceCheck] = []
    required = get_profile_specific_checks(profile)

    for name in required:
        if name == "healthz_ipv4":
            ipv4 = None
            if health_check is not None:
                ipv4 = _as_mapping(health_check.get("ipv4")) or (
                    health_check if health_check.get("family") == "ipv4" else None
                )
            ipv4 = ipv4 or _profile_payload(profile_checks, "healthz_ipv4")
            check = evaluate_healthz_payload(ipv4, label="healthz_ipv4")
            checks.append(
                AcceptanceCheck(check.name, check.status, check.details, profile)
            )
        elif name == "healthz_ipv6":
            ipv6 = None
            if health_check is not None:
                ipv6 = _as_mapping(health_check.get("ipv6")) or (
                    health_check if health_check.get("family") == "ipv6" else None
                )
            ipv6 = ipv6 or _profile_payload(profile_checks, "healthz_ipv6")
            check = evaluate_healthz_payload(ipv6, label="healthz_ipv6")
            checks.append(
                AcceptanceCheck(check.name, check.status, check.details, profile)
            )
        elif name == "spa_fallback":
            checks.append(
                _evaluate_boolean_check(
                    "spa_fallback",
                    profile,
                    _profile_payload(profile_checks, "spa_fallback"),
                    "works",
                    "success",
                    "ok",
                    pass_details="SPA fallback works",
                    fail_details="SPA fallback failed",
                )
            )
        elif name == "no_backend_api":
            checks.append(
                _evaluate_no_backend_api(
                    profile,
                    _profile_payload(profile_checks, "no_backend_api"),
                )
            )
        elif name == "sqlite_connection":
            checks.append(
                _evaluate_connection(
                    "sqlite_connection",
                    profile,
                    _profile_payload(profile_checks, "sqlite_connection", "db_connection"),
                )
            )
        elif name == "postgres_connection":
            checks.append(
                _evaluate_connection(
                    "postgres_connection",
                    profile,
                    _profile_payload(profile_checks, "postgres_connection", "db_connection"),
                )
            )
        elif name == "db_connection":
            checks.append(
                _evaluate_connection(
                    "db_connection",
                    profile,
                    _profile_payload(profile_checks, "db_connection"),
                )
            )
        elif name == "migration_success":
            checks.append(
                _evaluate_migration(
                    profile,
                    _profile_payload(profile_checks, "migration_success", "migration"),
                )
            )
        elif name == "persistence_across_redeploy":
            checks.append(
                _evaluate_persistence(
                    profile,
                    _profile_payload(
                        profile_checks,
                        "persistence_across_redeploy",
                        "persistence",
                    ),
                )
            )
        elif name == "identity_headers":
            checks.append(
                _evaluate_identity_headers(
                    profile,
                    _profile_payload(profile_checks, "identity_headers"),
                )
            )
        elif name == "ssr_rsc_render":
            checks.append(
                _evaluate_boolean_check(
                    "ssr_rsc_render",
                    profile,
                    _profile_payload(profile_checks, "ssr_rsc_render"),
                    "success",
                    "rendered",
                    "ok",
                    pass_details="SSR/RSC render succeeded",
                    fail_details="SSR/RSC render failed",
                )
            )
        elif name == "root_url_cookie_redirect":
            checks.append(
                _evaluate_root_url_cookie_redirect(
                    profile,
                    _profile_payload(profile_checks, "root_url_cookie_redirect"),
                )
            )
        elif name == "api_endpoints":
            checks.append(
                _evaluate_api_endpoints(
                    profile,
                    _profile_payload(profile_checks, "api_endpoints"),
                )
            )
        else:
            checks.append(
                AcceptanceCheck(
                    name=name,
                    status=AcceptanceStatus.FAIL,
                    details=f"no evaluator registered for check {name!r} (fail-closed)",
                    profile=profile,
                )
            )
    return checks


def determine_overall_status(checks: Sequence[AcceptanceCheck]) -> AcceptanceStatus:
    """FAIL if any required check failed or if there are no checks."""
    if not checks:
        return AcceptanceStatus.FAIL
    if any(check.status is AcceptanceStatus.FAIL for check in checks):
        return AcceptanceStatus.FAIL
    return AcceptanceStatus.PASS


def run_acceptance(
    profile: str,
    discovery_result: Optional[Dict[str, Any]] = None,
    health_check: Optional[Dict[str, Any]] = None,
    auth_check: Optional[Dict[str, Any]] = None,
    routing_check: Optional[Dict[str, Any]] = None,
    github_actions: Optional[Dict[str, Any]] = None,
    coolify_deployment: Optional[Dict[str, Any]] = None,
    container: Optional[Dict[str, Any]] = None,
    authenticated_access: Optional[Dict[str, Any]] = None,
    assets: Optional[Dict[str, Any]] = None,
    profile_checks: Optional[Dict[str, Any]] = None,
    routing_mode: Optional[str] = None,
) -> AcceptanceResult:
    """Run profile-specific post-deployment acceptance checks.

    All evidence must be supplied by the caller. This function never contacts
    remote systems and never treats missing data as success.
    """
    normalized_profile = (profile or "").strip()
    checks: List[AcceptanceCheck] = []

    if normalized_profile not in SUPPORTED_PROFILES:
        checks.append(
            AcceptanceCheck(
                name="profile_supported",
                status=AcceptanceStatus.FAIL,
                details=f"unknown or empty profile {profile!r} (fail-closed)",
                profile=profile or None,
            )
        )
        return AcceptanceResult(
            profile=profile or "",
            checks=checks,
            overall_status=AcceptanceStatus.FAIL,
        )

    resolved_mode = infer_routing_mode(normalized_profile, routing_mode)

    checks.append(_evaluate_discovery(discovery_result))
    gh_success, gh_workflows = _evaluate_github_actions(github_actions)
    checks.append(gh_success)
    checks.append(gh_workflows)
    checks.append(_evaluate_coolify(coolify_deployment))
    checks.append(_evaluate_container(container))
    health = evaluate_healthz_payload(health_check, label="healthz_ok")
    checks.append(AcceptanceCheck(health.name, health.status, health.details, normalized_profile))
    checks.append(_evaluate_auth_redirect(auth_check))
    checks.append(_evaluate_authenticated_access(authenticated_access))
    checks.append(_evaluate_routing(normalized_profile, routing_check, resolved_mode))
    checks.append(_evaluate_assets(assets))
    checks.extend(
        _evaluate_profile_specific(
            normalized_profile,
            health_check,
            profile_checks,
        )
    )

    return AcceptanceResult(
        profile=normalized_profile,
        checks=checks,
        overall_status=determine_overall_status(checks),
    )


def passing_common_evidence() -> Dict[str, Any]:
    """Return a complete set of passing common-check evidence for tests/fixtures.

    Values are synthetic. They are not live system results.
    """
    return {
        "discovery_result": {
            "success": True,
            "container_name": "coolify-discovered-abc123",
            "method": "coolify_api",
            "deployment_uuid": "dep-fixture-001",
        },
        "github_actions": {
            "success": True,
            "unexpected_workflows": [],
        },
        "coolify_deployment": {
            "success": True,
            "status": "finished",
        },
        "container": {
            "state": "running",
            "health": "healthy",
        },
        "health_check": {
            "status_code": 200,
            "body": {"ok": True},
            "ipv4": {"status_code": 200, "body": {"ok": True}},
            "ipv6": {"status_code": 200, "body": {"ok": True}},
        },
        "auth_check": {"status_code": 302},
        "authenticated_access": {"status_code": 200, "reachable": True},
        "routing_check": {
            "correct": True,
            "strip_prefix": True,
            "app_sees_root": True,
            "public_prefix": "/demo-app",
        },
        "assets": {"loaded": True, "failed_assets": []},
    }


def passing_profile_checks(profile: str) -> Dict[str, Any]:
    """Return synthetic passing profile-specific evidence."""
    identity = {
        "consumed": True,
        "all_required_present": True,
        "headers_present": list(IDENTITY_HEADERS),
    }
    persistence = {"write_ok": True, "read_after_redeploy": True}
    migration = {"applied": True, "failed": False}
    db = {"connected": True, "success": True}
    api = {"reachable": True, "endpoints": [{"path": "/healthz", "status_code": 200}]}

    mapping: Dict[str, Dict[str, Any]] = {
        "static-vite": {
            "spa_fallback": {"works": True},
            "no_backend_api": {"no_backend": True, "api_status_code": 404},
        },
        "react-hono-sqlite": {
            "sqlite_connection": dict(db),
            "migration_success": dict(migration),
            "persistence_across_redeploy": dict(persistence),
            "identity_headers": dict(identity),
        },
        "react-hono-postgres": {
            "postgres_connection": dict(db),
            "migration_success": dict(migration),
            "persistence_across_redeploy": dict(persistence),
            "identity_headers": dict(identity),
        },
        "next-postgres": {
            "ssr_rsc_render": {"success": True, "rendered": True},
            "root_url_cookie_redirect": {
                "root_url_ok": True,
                "cookie_ok": True,
                "redirect_ok": True,
            },
            "postgres_connection": dict(db),
            "identity_headers": dict(identity),
        },
        "python-fastapi": {
            "api_endpoints": dict(api),
            "db_connection": dict(db),
            "migration_success": dict(migration),
            "persistence_across_redeploy": dict(persistence),
        },
        "python-service": {
            "api_endpoints": dict(api),
            "db_connection": dict(db),
            "migration_success": dict(migration),
            "persistence_across_redeploy": dict(persistence),
        },
    }
    return mapping.get(profile, {})


def run_acceptance_from_evidence(profile: str, evidence: Mapping[str, Any]) -> AcceptanceResult:
    """Run acceptance from a single evidence mapping (CLI / fixtures)."""
    return run_acceptance(
        profile=profile,
        discovery_result=_as_mapping(evidence.get("discovery_result")),  # type: ignore[arg-type]
        health_check=_as_mapping(evidence.get("health_check")),  # type: ignore[arg-type]
        auth_check=_as_mapping(evidence.get("auth_check")),  # type: ignore[arg-type]
        routing_check=_as_mapping(evidence.get("routing_check")),  # type: ignore[arg-type]
        github_actions=_as_mapping(evidence.get("github_actions")),  # type: ignore[arg-type]
        coolify_deployment=_as_mapping(evidence.get("coolify_deployment")),  # type: ignore[arg-type]
        container=_as_mapping(evidence.get("container")),  # type: ignore[arg-type]
        authenticated_access=_as_mapping(evidence.get("authenticated_access")),  # type: ignore[arg-type]
        assets=_as_mapping(evidence.get("assets")),  # type: ignore[arg-type]
        profile_checks=_as_mapping(evidence.get("profile_checks")),  # type: ignore[arg-type]
        routing_mode=evidence.get("routing_mode") if isinstance(evidence.get("routing_mode"), str) else None,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Post-Deployment Acceptance — profile-specific portable-core checks"
    )
    parser.add_argument("--profile", help="Canonical app profile")
    parser.add_argument("--evidence-json", help="JSON object with collected evidence")
    parser.add_argument(
        "--list-checks",
        action="store_true",
        help="Print required checks for the profile and exit",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.list_checks:
        profile = (args.profile or "").strip()
        payload = {
            "profile": profile,
            "common_checks": get_common_checks(),
            "profile_specific_checks": get_profile_specific_checks(profile),
            "all_checks": get_all_required_checks(profile) if profile in SUPPORTED_PROFILES else [],
            "supported": profile in SUPPORTED_PROFILES,
        }
        print(json.dumps(payload, indent=2))
        return 0 if profile in SUPPORTED_PROFILES else 1

    if not args.profile:
        print(json.dumps({"error": "profile is required"}))
        return 1

    evidence: Dict[str, Any] = {}
    if args.evidence_json:
        try:
            loaded = json.loads(args.evidence_json)
        except json.JSONDecodeError as exc:
            print(json.dumps({"error": f"invalid evidence JSON: {exc}"}))
            return 1
        if not isinstance(loaded, dict):
            print(json.dumps({"error": "evidence JSON must be an object"}))
            return 1
        evidence = loaded

    result = run_acceptance_from_evidence(args.profile, evidence)
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.overall_status is AcceptanceStatus.PASS else 1


if __name__ == "__main__":
    sys.exit(main())
