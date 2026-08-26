#!/usr/bin/env python3
"""Tests for real_resource_discovery.py — Portable Core container discovery.

All network and Docker interactions are injected fakes. No live API calls.
Tokens used here are synthetic fixtures, not credentials.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

from real_resource_discovery import (
    DEFAULT_COOLIFY_URL,
    DiscoveryMethod,
    DiscoveryResult,
    build_coolify_deployments_url,
    build_docker_inspect_command,
    build_docker_ps_command,
    discover_container,
    discover_via_coolify_api,
    discover_via_docker_inspect,
    discover_via_docker_labels,
    extract_container_name,
    extract_deployment_uuid,
    extract_labels,
    parse_label_blob,
    redact_secrets,
    validate_app_uuid,
    validate_container_name,
    validate_coolify_url,
)


SCRIPT_DIR = Path(__file__).resolve().parent
MODULE_PATH = SCRIPT_DIR / "real_resource_discovery.py"
FORBIDDEN_AGENT_TERMS = ("hermes", "kanban", "agy", "codex", "opencode", "qwen")
FAKE_TOKEN = "test-token-not-a-credential"
APP_UUID = "0vbb6afzmevx9xdfl8hdfvd6"
DISCOVERED_NAME = "coolify-n4k8wq2p7m"


def _http(status: int, body: str, error: Optional[str] = None):
    def _impl(url: str, headers: Dict[str, str], timeout: int):
        _impl.calls.append({"url": url, "headers": headers, "timeout": timeout})
        return status, body, error

    _impl.calls = []
    return _impl


def _http_raise(exc: Exception):
    def _impl(url: str, headers: Dict[str, str], timeout: int):
        raise exc

    return _impl


def _cmd(exit_code: int, stdout: str = "", stderr: str = ""):
    def _impl(command: Sequence[str], timeout: int):
        _impl.calls.append({"command": list(command), "timeout": timeout})
        return exit_code, stdout, stderr

    _impl.calls = []
    return _impl


def _cmd_raise(exc: Exception):
    def _impl(command: Sequence[str], timeout: int):
        raise exc

    return _impl


class TestEnumsAndSerialization(unittest.TestCase):
    def test_discovery_method_values(self):
        self.assertEqual(DiscoveryMethod.COOLIFY_API.value, "coolify_api")
        self.assertEqual(DiscoveryMethod.DOCKER_LABELS.value, "docker_labels")
        self.assertEqual(DiscoveryMethod.DOCKER_INSPECT.value, "docker_inspect")

    def test_result_to_dict_success(self):
        result = DiscoveryResult(
            method=DiscoveryMethod.COOLIFY_API,
            container_name=DISCOVERED_NAME,
            deployment_uuid="dep-1",
            labels={"coolify.application.id": APP_UUID},
            success=True,
        )
        payload = result.to_dict()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["method"], "coolify_api")
        self.assertEqual(payload["container_name"], DISCOVERED_NAME)
        self.assertIsNone(payload["error"])
        json.dumps(payload)

    def test_result_to_dict_failure(self):
        result = DiscoveryResult(
            method=DiscoveryMethod.DOCKER_LABELS,
            container_name=None,
            deployment_uuid=None,
            labels={},
            success=False,
            error="no containers found",
        )
        payload = result.to_dict()
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"], "no containers found")


class TestValidators(unittest.TestCase):
    def test_valid_coolify_and_standard_ids(self):
        self.assertIsNone(validate_app_uuid(APP_UUID))
        self.assertIsNone(validate_app_uuid("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"))

    def test_invalid_uuid_rejected(self):
        self.assertIsNotNone(validate_app_uuid(""))
        self.assertIsNotNone(validate_app_uuid(None))
        self.assertIsNotNone(validate_app_uuid("../etc/passwd"))
        self.assertIsNotNone(validate_app_uuid("app/uuid"))
        self.assertIsNotNone(validate_app_uuid("has space"))

    def test_invalid_coolify_url_rejected(self):
        self.assertIsNotNone(validate_coolify_url(""))
        self.assertIsNotNone(validate_coolify_url("ftp://127.0.0.1"))
        self.assertIsNone(validate_coolify_url(DEFAULT_COOLIFY_URL))

    def test_invalid_container_name_rejected(self):
        self.assertIsNotNone(validate_container_name(""))
        self.assertIsNotNone(validate_container_name("../sneaky"))
        self.assertIsNone(validate_container_name(DISCOVERED_NAME))


class TestExtractionHelpers(unittest.TestCase):
    def test_extract_container_name_from_list(self):
        payload = [{"uuid": "dep-1", "container_name": DISCOVERED_NAME}]
        self.assertEqual(extract_container_name(payload), DISCOVERED_NAME)

    def test_extract_container_name_from_nested_containers(self):
        payload = {"data": [{"containers": [{"name": DISCOVERED_NAME}]}]}
        self.assertEqual(extract_container_name(payload), DISCOVERED_NAME)

    def test_extract_container_name_does_not_invent_slug_web(self):
        payload = [{"uuid": "dep-1", "status": "finished", "name": "finished"}]
        self.assertIsNone(extract_container_name(payload))

    def test_extract_deployment_uuid(self):
        payload = [{"deployment_uuid": "dep-fixture-22", "container_name": DISCOVERED_NAME}]
        self.assertEqual(extract_deployment_uuid(payload), "dep-fixture-22")

    def test_extract_labels_from_mapping(self):
        payload = {"labels": {"coolify.application.id": APP_UUID}}
        self.assertEqual(extract_labels(payload)["coolify.application.id"], APP_UUID)

    def test_parse_label_blob_json(self):
        raw = json.dumps({"coolify.application.id": APP_UUID, "com.docker.compose.service": "web"})
        labels = parse_label_blob(raw)
        self.assertEqual(labels["coolify.application.id"], APP_UUID)

    def test_parse_label_blob_csv_and_map(self):
        csv_labels = parse_label_blob(f"coolify.application.id={APP_UUID},owner=studio")
        self.assertEqual(csv_labels["owner"], "studio")
        map_labels = parse_label_blob(f"map[coolify.application.id:{APP_UUID} owner:studio]")
        self.assertEqual(map_labels["coolify.application.id"], APP_UUID)

    def test_redact_secrets_strips_bearer(self):
        text = redact_secrets(f"Authorization: Bearer {FAKE_TOKEN} leftover")
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertIn("***", text)


class TestCoolifyApiDiscovery(unittest.TestCase):
    def test_coolify_api_success(self):
        body = json.dumps(
            [
                {
                    "uuid": "dep-success-1",
                    "status": "finished",
                    "container_name": DISCOVERED_NAME,
                    "labels": {"coolify.application.id": APP_UUID},
                }
            ]
        )
        transport = _http(200, body)
        result = discover_via_coolify_api(
            APP_UUID,
            FAKE_TOKEN,
            DEFAULT_COOLIFY_URL,
            http_get=transport,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.container_name, DISCOVERED_NAME)
        self.assertEqual(result.deployment_uuid, "dep-success-1")
        self.assertEqual(result.method, DiscoveryMethod.COOLIFY_API)
        self.assertIn("/api/v1/applications/" + APP_UUID + "/deployments", transport.calls[0]["url"])
        self.assertTrue(transport.calls[0]["headers"]["Authorization"].startswith("Bearer "))

    def test_coolify_api_prefers_finished_deployment(self):
        body = json.dumps(
            [
                {"uuid": "dep-old", "status": "failed", "container_name": "coolify-old"},
                {"uuid": "dep-new", "status": "finished", "container_name": DISCOVERED_NAME},
            ]
        )
        result = discover_via_coolify_api(APP_UUID, FAKE_TOKEN, DEFAULT_COOLIFY_URL, http_get=_http(200, body))
        self.assertEqual(result.container_name, DISCOVERED_NAME)
        self.assertEqual(result.deployment_uuid, "dep-new")

    def test_coolify_api_http_401_fails(self):
        result = discover_via_coolify_api(
            APP_UUID,
            FAKE_TOKEN,
            DEFAULT_COOLIFY_URL,
            http_get=_http(401, "{}", "HTTP 401"),
        )
        self.assertFalse(result.success)
        self.assertIn("401", result.error or "")

    def test_coolify_api_http_500_fails(self):
        result = discover_via_coolify_api(
            APP_UUID,
            FAKE_TOKEN,
            DEFAULT_COOLIFY_URL,
            http_get=_http(500, "oops", "HTTP 500"),
        )
        self.assertFalse(result.success)

    def test_coolify_api_empty_list_fails(self):
        result = discover_via_coolify_api(
            APP_UUID,
            FAKE_TOKEN,
            DEFAULT_COOLIFY_URL,
            http_get=_http(200, "[]"),
        )
        self.assertFalse(result.success)
        self.assertIn("no deployments", result.error or "")

    def test_coolify_api_invalid_json_fails(self):
        result = discover_via_coolify_api(
            APP_UUID,
            FAKE_TOKEN,
            DEFAULT_COOLIFY_URL,
            http_get=_http(200, "<html>nope</html>"),
        )
        self.assertFalse(result.success)
        self.assertIn("JSON", result.error or "")

    def test_coolify_api_missing_token_fails(self):
        result = discover_via_coolify_api(APP_UUID, "", DEFAULT_COOLIFY_URL, http_get=_http(200, "[]"))
        self.assertFalse(result.success)
        self.assertIn("token", result.error or "")

    def test_coolify_api_invalid_uuid_does_not_call_transport(self):
        called = {"n": 0}

        def boom(url, headers, timeout):
            called["n"] += 1
            return 200, "[]", None

        result = discover_via_coolify_api("../bad", FAKE_TOKEN, DEFAULT_COOLIFY_URL, http_get=boom)
        self.assertFalse(result.success)
        self.assertEqual(called["n"], 0)

    def test_coolify_api_missing_container_name_fails_closed(self):
        body = json.dumps([{"uuid": "dep-1", "status": "finished"}])
        result = discover_via_coolify_api(APP_UUID, FAKE_TOKEN, DEFAULT_COOLIFY_URL, http_get=_http(200, body))
        self.assertFalse(result.success)
        self.assertIsNone(result.container_name)

    def test_coolify_api_transport_exception_fails_closed(self):
        result = discover_via_coolify_api(
            APP_UUID,
            FAKE_TOKEN,
            DEFAULT_COOLIFY_URL,
            http_get=_http_raise(RuntimeError("network down")),
        )
        self.assertFalse(result.success)
        self.assertIn("transport error", result.error or "")

    def test_coolify_api_timeout_status_zero_fails(self):
        result = discover_via_coolify_api(
            APP_UUID,
            FAKE_TOKEN,
            DEFAULT_COOLIFY_URL,
            http_get=_http(0, "", "request failed: timeout"),
        )
        self.assertFalse(result.success)
        self.assertIn("timeout", result.error or "")

    def test_token_not_copied_into_result(self):
        body = json.dumps([{"container_name": DISCOVERED_NAME, "uuid": "dep-x"}])
        result = discover_via_coolify_api(APP_UUID, FAKE_TOKEN, DEFAULT_COOLIFY_URL, http_get=_http(200, body))
        dumped = json.dumps(result.to_dict())
        self.assertNotIn(FAKE_TOKEN, dumped)


class TestDockerLabelDiscovery(unittest.TestCase):
    def test_docker_labels_success(self):
        stdout = f"{DISCOVERED_NAME}\tabc123\tUp 3 seconds (healthy)\tcoolify.application.id={APP_UUID}\n"
        transport = _cmd(0, stdout)
        result = discover_via_docker_labels(APP_UUID, run_command=transport)
        self.assertTrue(result.success)
        self.assertEqual(result.container_name, DISCOVERED_NAME)
        self.assertEqual(result.method, DiscoveryMethod.DOCKER_LABELS)
        command = transport.calls[0]["command"]
        self.assertEqual(command[:3], ["docker", "ps", "--filter"])
        self.assertIn(f"label=coolify.application.id={APP_UUID}", command)

    def test_docker_labels_no_containers_fails(self):
        result = discover_via_docker_labels(APP_UUID, run_command=_cmd(0, ""))
        self.assertFalse(result.success)
        self.assertIn("no containers", result.error or "")

    def test_docker_labels_command_failure(self):
        result = discover_via_docker_labels(APP_UUID, run_command=_cmd(1, "", "cannot connect to docker"))
        self.assertFalse(result.success)
        self.assertIn("cannot connect", result.error or "")

    def test_docker_labels_invalid_app_id(self):
        result = discover_via_docker_labels("", run_command=_cmd(0, "x"))
        self.assertFalse(result.success)

    def test_docker_labels_transport_exception(self):
        result = discover_via_docker_labels(APP_UUID, run_command=_cmd_raise(OSError("boom")))
        self.assertFalse(result.success)
        self.assertIn("transport error", result.error or "")

    def test_docker_labels_multiple_containers_uses_first(self):
        stdout = (
            f"{DISCOVERED_NAME}\tid1\tUp\tcoolify.application.id={APP_UUID}\n"
            f"coolify-other\tid2\tUp\tcoolify.application.id={APP_UUID}\n"
        )
        result = discover_via_docker_labels(APP_UUID, run_command=_cmd(0, stdout))
        self.assertEqual(result.container_name, DISCOVERED_NAME)


class TestDockerInspectDiscovery(unittest.TestCase):
    def test_docker_inspect_success(self):
        stdout = json.dumps({"coolify.application.id": APP_UUID, "managed-by": "coolify"})
        transport = _cmd(0, stdout)
        result = discover_via_docker_inspect(DISCOVERED_NAME, run_command=transport)
        self.assertTrue(result.success)
        self.assertEqual(result.container_name, DISCOVERED_NAME)
        self.assertEqual(result.labels["coolify.application.id"], APP_UUID)
        self.assertIn("inspect", transport.calls[0]["command"])

    def test_docker_inspect_missing_container(self):
        result = discover_via_docker_inspect(
            DISCOVERED_NAME,
            run_command=_cmd(1, "", "Error: No such object"),
        )
        self.assertFalse(result.success)
        self.assertIn("No such object", result.error or "")

    def test_docker_inspect_invalid_json_fails(self):
        result = discover_via_docker_inspect(DISCOVERED_NAME, run_command=_cmd(0, "not-labels"))
        self.assertFalse(result.success)

    def test_docker_inspect_requires_name(self):
        result = discover_via_docker_inspect("", run_command=_cmd(0, "{}"))
        self.assertFalse(result.success)
        self.assertIn("required", result.error or "")

    def test_docker_inspect_empty_labels_fail_closed(self):
        result = discover_via_docker_inspect(DISCOVERED_NAME, run_command=_cmd(0, "{}"))
        self.assertFalse(result.success)


class TestDiscoverContainerFallback(unittest.TestCase):
    def test_primary_api_falls_back_to_labels(self):
        http = _http(500, "", "HTTP 500")
        docker = _cmd(0, f"{DISCOVERED_NAME}\tid\tUp\tcoolify.application.id={APP_UUID}\n")
        result = discover_container(
            APP_UUID,
            DiscoveryMethod.COOLIFY_API,
            api_token=FAKE_TOKEN,
            coolify_url=DEFAULT_COOLIFY_URL,
            http_get=http,
            run_command=docker,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.method, DiscoveryMethod.DOCKER_LABELS)
        self.assertEqual(result.container_name, DISCOVERED_NAME)

    def test_all_methods_fail_closed(self):
        result = discover_container(
            APP_UUID,
            DiscoveryMethod.COOLIFY_API,
            api_token=FAKE_TOKEN,
            coolify_url=DEFAULT_COOLIFY_URL,
            http_get=_http(404, "{}", "HTTP 404"),
            run_command=_cmd(1, "", "docker unavailable"),
            container_name=DISCOVERED_NAME,
        )
        self.assertFalse(result.success)
        self.assertIn("coolify_api", result.error or "")
        self.assertIn("docker_labels", result.error or "")

    def test_no_fallback_keeps_primary_error(self):
        result = discover_container(
            APP_UUID,
            DiscoveryMethod.COOLIFY_API,
            api_token="",
            fallback=False,
            run_command=_cmd(0, f"{DISCOVERED_NAME}\tid\tUp\tlabel=1\n"),
        )
        self.assertFalse(result.success)
        self.assertEqual(result.method, DiscoveryMethod.COOLIFY_API)

    def test_unknown_method_fails_closed(self):
        result = discover_container(APP_UUID, "telepathy")  # type: ignore[arg-type]
        self.assertFalse(result.success)
        self.assertIn("unknown discovery method", result.error or "")

    def test_default_method_is_coolify_api(self):
        body = json.dumps([{"container_name": DISCOVERED_NAME, "uuid": "dep-default"}])
        result = discover_container(
            APP_UUID,
            api_token=FAKE_TOKEN,
            coolify_url=DEFAULT_COOLIFY_URL,
            http_get=_http(200, body),
            fallback=False,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.method, DiscoveryMethod.COOLIFY_API)


class TestCommandBuildersAndPortability(unittest.TestCase):
    def test_url_builder(self):
        url = build_coolify_deployments_url("http://127.0.0.1:8000/", APP_UUID)
        self.assertEqual(url, f"http://127.0.0.1:8000/api/v1/applications/{APP_UUID}/deployments")

    def test_docker_ps_devbox_wrapper(self):
        command = build_docker_ps_command(APP_UUID, use_local_vm=True)
        self.assertEqual(command[:3], ["devbox", "ssh", "--"])
        self.assertIn("docker", command)

    def test_docker_inspect_command_uses_supplied_name_only(self):
        command = build_docker_inspect_command(DISCOVERED_NAME)
        self.assertEqual(command[0:3], ["docker", "inspect", DISCOVERED_NAME])
        self.assertNotIn("demo-app-web", command)

    def test_source_is_agent_neutral(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        lowered = source.lower()
        for term in FORBIDDEN_AGENT_TERMS:
            self.assertNotIn(term, lowered)

    def test_source_never_hardcodes_slug_web_names(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("{slug}-web", source)
        self.assertNotIn("slug}-web", source)
        self.assertNotIn("-web ...", source)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                rendered = ast.unparse(node)
                self.assertNotIn("-web", rendered)

    def test_source_is_stdlib_only(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        allowed = {
            "argparse",
            "json",
            "os",
            "re",
            "subprocess",
            "sys",
            "urllib",
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

    def test_cli_invalid_uuid_fails_without_network(self):
        completed = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--app-uuid", "../bad", "--no-fallback"],
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": "/usr/bin:/bin", "COOLIFY_API_TOKEN": ""},
        )
        self.assertNotEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["success"])
        self.assertIsNone(payload["container_name"])


class TestIntegrationWithAcceptance(unittest.TestCase):
    def test_discovery_then_acceptance_pass(self):
        from post_deploy_acceptance import (
            AcceptanceStatus,
            passing_common_evidence,
            passing_profile_checks,
            run_acceptance,
        )

        body = json.dumps(
            [{"uuid": "dep-int-1", "status": "finished", "container_name": DISCOVERED_NAME}]
        )
        discovery = discover_via_coolify_api(
            APP_UUID,
            FAKE_TOKEN,
            DEFAULT_COOLIFY_URL,
            http_get=_http(200, body),
        )
        self.assertTrue(discovery.success)
        evidence = passing_common_evidence()
        result = run_acceptance(
            "python-fastapi",
            discovery_result=discovery.to_dict(),
            health_check=evidence["health_check"],
            auth_check=evidence["auth_check"],
            routing_check=evidence["routing_check"],
            github_actions=evidence["github_actions"],
            coolify_deployment=evidence["coolify_deployment"],
            container=evidence["container"],
            authenticated_access=evidence["authenticated_access"],
            assets=evidence["assets"],
            profile_checks=passing_profile_checks("python-fastapi"),
        )
        self.assertEqual(result.overall_status, AcceptanceStatus.PASS)

    def test_discovery_error_propagates_to_acceptance(self):
        from post_deploy_acceptance import AcceptanceStatus, run_acceptance

        discovery = discover_via_docker_labels("bad id", run_command=_cmd(0, ""))
        result = run_acceptance("static-vite", discovery_result=discovery.to_dict())
        self.assertEqual(result.overall_status, AcceptanceStatus.FAIL)
        discovery_check = next(check for check in result.checks if check.name == "resource_discovery")
        self.assertEqual(discovery_check.status, AcceptanceStatus.FAIL)


if __name__ == "__main__":
    unittest.main()
