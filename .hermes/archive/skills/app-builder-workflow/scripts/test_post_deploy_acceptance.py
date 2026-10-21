#!/usr/bin/env python3
"""Tests for post_deploy_acceptance.py — Portable Core post-deploy gates.

All tests use synthetic evidence only. No live HTTP, Docker, or Coolify calls.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import unittest
from pathlib import Path

from post_deploy_acceptance import (
    COMMON_CHECKS,
    IDENTITY_HEADERS,
    PROFILE_SPECIFIC_CHECKS,
    SUPPORTED_PROFILES,
    AcceptanceCheck,
    AcceptanceResult,
    AcceptanceStatus,
    determine_overall_status,
    evaluate_healthz_payload,
    get_all_required_checks,
    get_common_checks,
    get_profile_specific_checks,
    infer_routing_mode,
    passing_common_evidence,
    passing_profile_checks,
    run_acceptance,
    run_acceptance_from_evidence,
)


SCRIPT_DIR = Path(__file__).resolve().parent
MODULE_PATH = SCRIPT_DIR / "post_deploy_acceptance.py"
FORBIDDEN_AGENT_TERMS = ("hermes", "kanban", "agy", "codex", "opencode", "qwen")


def _run(profile: str, **overrides):
    evidence = passing_common_evidence()
    evidence.update(overrides)
    profile_checks = overrides.get("profile_checks", passing_profile_checks(profile))
    return run_acceptance(
        profile,
        discovery_result=evidence.get("discovery_result"),
        health_check=evidence.get("health_check"),
        auth_check=evidence.get("auth_check"),
        routing_check=evidence.get("routing_check"),
        github_actions=evidence.get("github_actions"),
        coolify_deployment=evidence.get("coolify_deployment"),
        container=evidence.get("container"),
        authenticated_access=evidence.get("authenticated_access"),
        assets=evidence.get("assets"),
        profile_checks=profile_checks,
        routing_mode=evidence.get("routing_mode"),
    )


def _status_by_name(result: AcceptanceResult) -> dict:
    return {check.name: check.status for check in result.checks}


class TestAcceptanceEnumsAndSerialization(unittest.TestCase):
    def test_acceptance_status_values(self):
        self.assertEqual(AcceptanceStatus.PASS.value, "pass")
        self.assertEqual(AcceptanceStatus.FAIL.value, "fail")
        self.assertEqual(AcceptanceStatus.SKIP.value, "skip")

    def test_check_to_dict_omits_optional_none(self):
        check = AcceptanceCheck(name="healthz_ok", status=AcceptanceStatus.PASS)
        payload = check.to_dict()
        self.assertEqual(payload["name"], "healthz_ok")
        self.assertEqual(payload["status"], "pass")
        self.assertNotIn("details", payload)
        self.assertNotIn("profile", payload)

    def test_check_to_dict_includes_optional_fields(self):
        check = AcceptanceCheck(
            name="sqlite_connection",
            status=AcceptanceStatus.FAIL,
            details="missing",
            profile="react-hono-sqlite",
        )
        payload = check.to_dict()
        self.assertEqual(payload["details"], "missing")
        self.assertEqual(payload["profile"], "react-hono-sqlite")

    def test_result_to_dict_and_failed_checks(self):
        result = AcceptanceResult(
            profile="static-vite",
            checks=[
                AcceptanceCheck("a", AcceptanceStatus.PASS),
                AcceptanceCheck("b", AcceptanceStatus.FAIL, details="nope"),
            ],
            overall_status=AcceptanceStatus.FAIL,
        )
        payload = result.to_dict()
        self.assertEqual(payload["profile"], "static-vite")
        self.assertEqual(payload["overall_status"], "fail")
        self.assertEqual(len(payload["checks"]), 2)
        self.assertEqual(len(result.failed_checks()), 1)
        json.dumps(payload)

    def test_result_json_roundtrip_keys(self):
        result = _run("static-vite")
        payload = json.loads(json.dumps(result.to_dict()))
        self.assertEqual(payload["overall_status"], "pass")
        self.assertEqual(payload["profile"], "static-vite")
        self.assertGreaterEqual(len(payload["checks"]), len(COMMON_CHECKS))


class TestProfileCheckRegistry(unittest.TestCase):
    def test_all_six_profiles_have_specific_checks(self):
        self.assertEqual(len(SUPPORTED_PROFILES), 6)
        for profile in SUPPORTED_PROFILES:
            checks = get_profile_specific_checks(profile)
            self.assertGreaterEqual(len(checks), 3, profile)
            self.assertEqual(tuple(checks), PROFILE_SPECIFIC_CHECKS[profile])

    def test_common_checks_cover_contract(self):
        names = get_common_checks()
        for required in (
            "resource_discovery",
            "github_actions_success",
            "coolify_deployment_success",
            "container_running_healthy",
            "healthz_ok",
            "authentik_redirect_unauthenticated",
            "authenticated_access",
            "strip_prefix_path_routing",
            "assets_load",
        ):
            self.assertIn(required, names)

    def test_unknown_profile_has_no_specific_checks(self):
        self.assertEqual(get_profile_specific_checks("does-not-exist"), [])

    def test_all_required_checks_combine_common_and_specific(self):
        checks = get_all_required_checks("react-hono-sqlite")
        self.assertEqual(checks[: len(COMMON_CHECKS)], list(COMMON_CHECKS))
        self.assertIn("sqlite_connection", checks)
        self.assertNotIn("ssr_rsc_render", checks)

    def test_routing_mode_defaults(self):
        self.assertEqual(infer_routing_mode("static-vite"), "path")
        self.assertEqual(infer_routing_mode("next-postgres"), "subdomain")
        self.assertEqual(infer_routing_mode("python-service"), "subdomain")
        self.assertEqual(infer_routing_mode("static-vite", "subdomain"), "subdomain")
        self.assertEqual(infer_routing_mode("static-vite", "weird"), "unknown")


class TestValidProfilesPass(unittest.TestCase):
    def test_static_vite_passes(self):
        result = _run("static-vite")
        self.assertEqual(result.overall_status, AcceptanceStatus.PASS)
        names = {check.name for check in result.checks}
        self.assertTrue({"healthz_ipv4", "healthz_ipv6", "spa_fallback", "no_backend_api"} <= names)

    def test_react_hono_sqlite_passes(self):
        result = _run("react-hono-sqlite")
        self.assertEqual(result.overall_status, AcceptanceStatus.PASS)
        self.assertEqual(_status_by_name(result)["sqlite_connection"], AcceptanceStatus.PASS)

    def test_react_hono_postgres_passes(self):
        result = _run("react-hono-postgres")
        self.assertEqual(result.overall_status, AcceptanceStatus.PASS)
        self.assertEqual(_status_by_name(result)["postgres_connection"], AcceptanceStatus.PASS)

    def test_next_postgres_passes_and_skips_strip_prefix(self):
        result = _run("next-postgres")
        self.assertEqual(result.overall_status, AcceptanceStatus.PASS)
        # subdomain profile without forcing path routing still receives routing evidence
        # in the fixture; explicit skip is tested separately.
        self.assertEqual(_status_by_name(result)["ssr_rsc_render"], AcceptanceStatus.PASS)

    def test_next_postgres_skips_strip_prefix_when_unprovided(self):
        result = _run("next-postgres", routing_check=None)
        self.assertEqual(_status_by_name(result)["strip_prefix_path_routing"], AcceptanceStatus.SKIP)
        self.assertEqual(result.overall_status, AcceptanceStatus.PASS)

    def test_python_fastapi_passes(self):
        result = _run("python-fastapi")
        self.assertEqual(result.overall_status, AcceptanceStatus.PASS)
        self.assertEqual(_status_by_name(result)["api_endpoints"], AcceptanceStatus.PASS)

    def test_python_service_passes(self):
        result = _run("python-service")
        self.assertEqual(result.overall_status, AcceptanceStatus.PASS)
        self.assertEqual(_status_by_name(result)["db_connection"], AcceptanceStatus.PASS)

    def test_same_input_is_deterministic(self):
        first = _run("react-hono-postgres").to_dict()
        second = _run("react-hono-postgres").to_dict()
        self.assertEqual(first, second)


class TestHealthAndAuthFailures(unittest.TestCase):
    def test_missing_health_check_fails_closed(self):
        result = _run("static-vite", health_check=None)
        self.assertEqual(result.overall_status, AcceptanceStatus.FAIL)
        self.assertEqual(_status_by_name(result)["healthz_ok"], AcceptanceStatus.FAIL)

    def test_health_http_503_fails(self):
        result = _run("static-vite", health_check={"status_code": 503, "body": {"ok": True}})
        self.assertEqual(_status_by_name(result)["healthz_ok"], AcceptanceStatus.FAIL)

    def test_health_body_not_ok_fails(self):
        result = _run("static-vite", health_check={"status_code": 200, "body": {"ok": False}})
        self.assertEqual(_status_by_name(result)["healthz_ok"], AcceptanceStatus.FAIL)

    def test_health_body_string_true_is_accepted(self):
        check = evaluate_healthz_payload({"status_code": 200, "body": '{"ok":true}'})
        self.assertEqual(check.status, AcceptanceStatus.PASS)

    def test_health_body_html_fails(self):
        check = evaluate_healthz_payload({"status_code": 200, "body": "<html>ok</html>"})
        self.assertEqual(check.status, AcceptanceStatus.FAIL)

    def test_auth_redirect_must_be_302(self):
        result = _run("static-vite", auth_check={"status_code": 200})
        self.assertEqual(_status_by_name(result)["authentik_redirect_unauthenticated"], AcceptanceStatus.FAIL)
        self.assertEqual(result.overall_status, AcceptanceStatus.FAIL)

    def test_authenticated_access_missing_fails(self):
        result = _run("static-vite", authenticated_access=None)
        self.assertEqual(_status_by_name(result)["authenticated_access"], AcceptanceStatus.FAIL)

    def test_assets_failed_list_fails(self):
        result = _run("static-vite", assets={"loaded": False, "failed_assets": ["/app.js"]})
        self.assertEqual(_status_by_name(result)["assets_load"], AcceptanceStatus.FAIL)


class TestDiscoveryAndDeployFailures(unittest.TestCase):
    def test_missing_discovery_fails_closed(self):
        result = _run("python-fastapi", discovery_result=None)
        self.assertEqual(_status_by_name(result)["resource_discovery"], AcceptanceStatus.FAIL)
        self.assertEqual(result.overall_status, AcceptanceStatus.FAIL)

    def test_unsuccessful_discovery_fails(self):
        result = _run(
            "python-fastapi",
            discovery_result={"success": False, "error": "no containers", "container_name": None},
        )
        self.assertEqual(_status_by_name(result)["resource_discovery"], AcceptanceStatus.FAIL)

    def test_discovery_without_container_name_fails(self):
        result = _run("python-fastapi", discovery_result={"success": True, "container_name": ""})
        self.assertEqual(_status_by_name(result)["resource_discovery"], AcceptanceStatus.FAIL)

    def test_container_not_healthy_fails(self):
        result = _run("static-vite", container={"state": "running", "health": "unhealthy"})
        self.assertEqual(_status_by_name(result)["container_running_healthy"], AcceptanceStatus.FAIL)

    def test_container_combined_status_accepted(self):
        result = _run("static-vite", container={"status": "running:healthy"})
        self.assertEqual(_status_by_name(result)["container_running_healthy"], AcceptanceStatus.PASS)

    def test_github_actions_failure(self):
        result = _run("static-vite", github_actions={"success": False, "unexpected_workflows": []})
        self.assertEqual(_status_by_name(result)["github_actions_success"], AcceptanceStatus.FAIL)

    def test_unexpected_workflows_fail(self):
        result = _run(
            "static-vite",
            github_actions={"success": True, "unexpected_workflows": ["deploy-ref-app.yml"]},
        )
        self.assertEqual(_status_by_name(result)["expected_workflows_only"], AcceptanceStatus.FAIL)

    def test_coolify_deploy_failure(self):
        result = _run("static-vite", coolify_deployment={"success": False, "status": "failed"})
        self.assertEqual(_status_by_name(result)["coolify_deployment_success"], AcceptanceStatus.FAIL)


class TestProfileSpecificFailures(unittest.TestCase):
    def test_unknown_profile_fails_closed(self):
        result = run_acceptance("not-a-profile")
        self.assertEqual(result.overall_status, AcceptanceStatus.FAIL)
        self.assertEqual(result.checks[0].name, "profile_supported")

    def test_empty_profile_fails_closed(self):
        result = run_acceptance("")
        self.assertEqual(result.overall_status, AcceptanceStatus.FAIL)

    def test_missing_profile_specific_evidence_fails(self):
        result = _run("react-hono-sqlite", profile_checks={})
        names = _status_by_name(result)
        self.assertEqual(names["sqlite_connection"], AcceptanceStatus.FAIL)
        self.assertEqual(names["migration_success"], AcceptanceStatus.FAIL)
        self.assertEqual(result.overall_status, AcceptanceStatus.FAIL)

    def test_sqlite_persistence_requires_readback(self):
        checks = passing_profile_checks("react-hono-sqlite")
        checks["persistence_across_redeploy"] = {"write_ok": True, "read_after_redeploy": False}
        result = _run("react-hono-sqlite", profile_checks=checks)
        self.assertEqual(_status_by_name(result)["persistence_across_redeploy"], AcceptanceStatus.FAIL)

    def test_postgres_migration_failed_flag(self):
        checks = passing_profile_checks("react-hono-postgres")
        checks["migration_success"] = {"applied": True, "failed": True}
        result = _run("react-hono-postgres", profile_checks=checks)
        self.assertEqual(_status_by_name(result)["migration_success"], AcceptanceStatus.FAIL)

    def test_identity_headers_require_consumption(self):
        checks = passing_profile_checks("react-hono-sqlite")
        checks["identity_headers"] = {
            "consumed": False,
            "headers_present": list(IDENTITY_HEADERS),
        }
        result = _run("react-hono-sqlite", profile_checks=checks)
        self.assertEqual(_status_by_name(result)["identity_headers"], AcceptanceStatus.FAIL)

    def test_static_vite_backend_api_detected(self):
        checks = passing_profile_checks("static-vite")
        checks["no_backend_api"] = {"no_backend": False, "backend_requests": 3}
        result = _run("static-vite", profile_checks=checks)
        self.assertEqual(_status_by_name(result)["no_backend_api"], AcceptanceStatus.FAIL)

    def test_static_vite_spa_fallback_required(self):
        checks = passing_profile_checks("static-vite")
        checks["spa_fallback"] = {"works": False}
        result = _run("static-vite", profile_checks=checks)
        self.assertEqual(_status_by_name(result)["spa_fallback"], AcceptanceStatus.FAIL)

    def test_next_postgres_ssr_failure(self):
        checks = passing_profile_checks("next-postgres")
        checks["ssr_rsc_render"] = {"success": False}
        result = _run("next-postgres", profile_checks=checks)
        self.assertEqual(_status_by_name(result)["ssr_rsc_render"], AcceptanceStatus.FAIL)

    def test_next_postgres_cookie_redirect_partial_fail(self):
        checks = passing_profile_checks("next-postgres")
        checks["root_url_cookie_redirect"] = {
            "root_url_ok": True,
            "cookie_ok": False,
            "redirect_ok": True,
        }
        result = _run("next-postgres", profile_checks=checks)
        self.assertEqual(_status_by_name(result)["root_url_cookie_redirect"], AcceptanceStatus.FAIL)

    def test_python_api_endpoints_list(self):
        checks = passing_profile_checks("python-fastapi")
        checks["api_endpoints"] = {
            "endpoints": [
                {"path": "/items", "status_code": 200},
                {"path": "/ready", "ok": True},
            ]
        }
        result = _run("python-fastapi", profile_checks=checks)
        self.assertEqual(_status_by_name(result)["api_endpoints"], AcceptanceStatus.PASS)

    def test_python_api_endpoints_bad_item_fails(self):
        checks = passing_profile_checks("python-service")
        checks["api_endpoints"] = {"endpoints": [{"path": "/items", "status_code": 500}]}
        result = _run("python-service", profile_checks=checks)
        self.assertEqual(_status_by_name(result)["api_endpoints"], AcceptanceStatus.FAIL)

    def test_unknown_routing_mode_fails_closed(self):
        result = _run("static-vite", routing_mode="weird")
        self.assertEqual(_status_by_name(result)["strip_prefix_path_routing"], AcceptanceStatus.FAIL)

    def test_path_profile_missing_routing_fails(self):
        result = _run("static-vite", routing_check=None)
        self.assertEqual(_status_by_name(result)["strip_prefix_path_routing"], AcceptanceStatus.FAIL)


class TestOverallStatusAndEvidenceHelper(unittest.TestCase):
    def test_empty_checks_fail_closed(self):
        self.assertEqual(determine_overall_status([]), AcceptanceStatus.FAIL)

    def test_skip_and_pass_is_overall_pass(self):
        checks = [
            AcceptanceCheck("a", AcceptanceStatus.PASS),
            AcceptanceCheck("b", AcceptanceStatus.SKIP),
        ]
        self.assertEqual(determine_overall_status(checks), AcceptanceStatus.PASS)

    def test_any_fail_is_overall_fail(self):
        checks = [
            AcceptanceCheck("a", AcceptanceStatus.PASS),
            AcceptanceCheck("b", AcceptanceStatus.FAIL),
        ]
        self.assertEqual(determine_overall_status(checks), AcceptanceStatus.FAIL)

    def test_run_acceptance_from_evidence(self):
        evidence = passing_common_evidence()
        evidence["profile_checks"] = passing_profile_checks("static-vite")
        result = run_acceptance_from_evidence("static-vite", evidence)
        self.assertEqual(result.overall_status, AcceptanceStatus.PASS)


class TestPortabilityAndCli(unittest.TestCase):
    def test_source_is_agent_neutral(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        lowered = source.lower()
        for term in FORBIDDEN_AGENT_TERMS:
            self.assertNotIn(term, lowered)

    def test_source_is_stdlib_only(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        allowed = {
            "argparse",
            "json",
            "sys",
            "dataclasses",
            "enum",
            "typing",
            "__future__",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], allowed)
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertIn(node.module.split(".")[0], allowed)

    def test_cli_list_checks(self):
        completed = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--list-checks", "--profile", "static-vite"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["supported"])
        self.assertIn("spa_fallback", payload["profile_specific_checks"])

    def test_cli_unknown_profile_lists_fail(self):
        completed = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--list-checks", "--profile", "nope"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)

    def test_cli_acceptance_with_evidence_json(self):
        evidence = passing_common_evidence()
        evidence["profile_checks"] = passing_profile_checks("python-service")
        completed = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                "--profile",
                "python-service",
                "--evidence-json",
                json.dumps(evidence),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["overall_status"], "pass")

    def test_cli_missing_evidence_fails_closed(self):
        completed = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--profile", "static-vite"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["overall_status"], "fail")


class TestIntegrationDiscoveryToAcceptance(unittest.TestCase):
    def test_failed_discovery_blocks_acceptance(self):
        from real_resource_discovery import DiscoveryMethod, DiscoveryResult

        discovery = DiscoveryResult(
            method=DiscoveryMethod.COOLIFY_API,
            container_name=None,
            deployment_uuid=None,
            labels={},
            success=False,
            error="no deployments found",
        )
        result = run_acceptance(
            "static-vite",
            discovery_result=discovery.to_dict(),
            health_check={"status_code": 200, "body": {"ok": True}},
            auth_check={"status_code": 302},
            profile_checks=passing_profile_checks("static-vite"),
        )
        self.assertEqual(result.overall_status, AcceptanceStatus.FAIL)
        self.assertEqual(_status_by_name(result)["resource_discovery"], AcceptanceStatus.FAIL)

    def test_successful_discovery_feeds_acceptance(self):
        from real_resource_discovery import DiscoveryMethod, DiscoveryResult

        discovery = DiscoveryResult(
            method=DiscoveryMethod.DOCKER_LABELS,
            container_name="coolify-live-container-9",
            deployment_uuid="dep-live-9",
            labels={"coolify.application.id": "app-live-9"},
            success=True,
        )
        evidence = passing_common_evidence()
        result = run_acceptance(
            "react-hono-sqlite",
            discovery_result=discovery.to_dict(),
            health_check=evidence["health_check"],
            auth_check=evidence["auth_check"],
            routing_check=evidence["routing_check"],
            github_actions=evidence["github_actions"],
            coolify_deployment=evidence["coolify_deployment"],
            container=evidence["container"],
            authenticated_access=evidence["authenticated_access"],
            assets=evidence["assets"],
            profile_checks=passing_profile_checks("react-hono-sqlite"),
        )
        self.assertEqual(result.overall_status, AcceptanceStatus.PASS)
        discovery_check = next(check for check in result.checks if check.name == "resource_discovery")
        self.assertIn("coolify-live-container-9", discovery_check.details or "")


if __name__ == "__main__":
    unittest.main()
