#!/usr/bin/env python3
"""
real_resource_discovery.py — Coolify Container Discovery.

Agent-neutral, stdlib-only Python for Portable App Builder Core.

Discovers container names dynamically. Never invents names such as
``<slug>-web`` — those do not exist on Coolify v5.

Canonical methods:
1. Coolify API: GET /api/v1/applications/<uuid>/deployments
2. Docker labels: docker ps --filter label=coolify.application.id=<app-id>
3. Docker inspect: docker inspect <container-name>

All transports are injectable so tests can run without live API or Docker
calls. Missing credentials, invalid identifiers, empty results, and transport
errors fail closed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse


class DiscoveryMethod(str, Enum):
    """Supported container discovery methods."""

    COOLIFY_API = "coolify_api"
    DOCKER_LABELS = "docker_labels"
    DOCKER_INSPECT = "docker_inspect"


HttpGet = Callable[[str, Dict[str, str], int], Tuple[int, str, Optional[str]]]
CommandRunner = Callable[[Sequence[str], int], Tuple[int, str, str]]

STANDARD_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
COOLIFY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")
CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,253}$")

DEFAULT_COOLIFY_URL = "http://127.0.0.1:8000"
DEFAULT_HTTP_TIMEOUT = 10
DEFAULT_COMMAND_TIMEOUT = 15

# Fields that may hold a real container name in Coolify-like payloads.
_CONTAINER_NAME_KEYS = (
    "container_name",
    "containerName",
    "discovered_container_name",
)


@dataclass(frozen=True)
class DiscoveryResult:
    """Result of a single discovery attempt."""

    method: DiscoveryMethod
    container_name: Optional[str]
    deployment_uuid: Optional[str]
    labels: Dict[str, str] = field(default_factory=dict)
    success: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "method": self.method.value,
            "container_name": self.container_name,
            "deployment_uuid": self.deployment_uuid,
            "labels": dict(self.labels),
            "success": self.success,
            "error": self.error,
        }


def _fail(
    method: DiscoveryMethod,
    error: str,
    *,
    labels: Optional[Dict[str, str]] = None,
    deployment_uuid: Optional[str] = None,
    container_name: Optional[str] = None,
) -> DiscoveryResult:
    return DiscoveryResult(
        method=method,
        container_name=container_name,
        deployment_uuid=deployment_uuid,
        labels=dict(labels or {}),
        success=False,
        error=error,
    )


def _ok(
    method: DiscoveryMethod,
    container_name: str,
    *,
    labels: Optional[Dict[str, str]] = None,
    deployment_uuid: Optional[str] = None,
) -> DiscoveryResult:
    return DiscoveryResult(
        method=method,
        container_name=container_name,
        deployment_uuid=deployment_uuid,
        labels=dict(labels or {}),
        success=True,
        error=None,
    )


def redact_secrets(text: str) -> str:
    """Remove bearer tokens and authorization values from error text."""
    if not text:
        return text
    redacted = re.sub(r"(?i)(bearer\s+)\S+", r"\1***", text)
    redacted = re.sub(r"(?i)(authorization\s*[:=]\s*)\S+", r"\1***", redacted)
    redacted = re.sub(r"(?i)(api[_-]?token\s*[:=]\s*)\S+", r"\1***", redacted)
    return redacted


def validate_app_uuid(app_uuid: Optional[str]) -> Optional[str]:
    """Return an error message if the application id is invalid, else None."""
    if app_uuid is None:
        return "app uuid is required"
    value = str(app_uuid).strip()
    if not value:
        return "app uuid is required"
    if any(part in value for part in ("/", "\\", " ", "?", "#", "@", ":", "..")):
        return "app uuid contains invalid characters"
    if not (STANDARD_UUID_RE.match(value) or COOLIFY_ID_RE.match(value)):
        return "app uuid format is invalid"
    return None


def validate_coolify_url(coolify_url: Optional[str]) -> Optional[str]:
    """Return an error message if the Coolify base URL is invalid, else None."""
    if not coolify_url or not str(coolify_url).strip():
        return "coolify url is required"
    parsed = urlparse(str(coolify_url).strip())
    if parsed.scheme not in {"http", "https"}:
        return "coolify url must be http or https"
    if not parsed.netloc:
        return "coolify url is missing a host"
    return None


def validate_container_name(container_name: Optional[str]) -> Optional[str]:
    """Return an error message if a supplied inspect target is invalid."""
    if container_name is None or not str(container_name).strip():
        return "container name is required"
    value = str(container_name).strip()
    if any(part in value for part in ("/", "\\", " ", "..")):
        return "container name contains invalid characters"
    if not CONTAINER_NAME_RE.match(value):
        return "container name format is invalid"
    return None


def default_http_get(url: str, headers: Dict[str, str], timeout: int) -> Tuple[int, str, Optional[str]]:
    """stdlib HTTP GET. Authorization values are never returned in errors."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return int(response.getcode()), body, None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return int(exc.code), body, f"HTTP {exc.code}"
    except urllib.error.URLError:
        return 0, "", "request failed: URLError"
    except TimeoutError:
        return 0, "", "request failed: timeout"
    except Exception as exc:  # noqa: BLE001 — fail-closed transport wrapper
        return 0, "", f"request failed: {type(exc).__name__}"


def default_run_command(command: Sequence[str], timeout: int) -> Tuple[int, str, str]:
    """stdlib subprocess runner used by Docker discovery methods."""
    if not command:
        return 1, "", "command is empty"
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return completed.returncode, completed.stdout or "", completed.stderr or ""
    except subprocess.TimeoutExpired:
        return 1, "", "command timed out"
    except FileNotFoundError:
        return 1, "", f"command not found: {command[0]}"
    except Exception as exc:  # noqa: BLE001 — fail-closed transport wrapper
        return 1, "", f"command failed: {type(exc).__name__}"


def build_coolify_deployments_url(coolify_url: str, app_uuid: str) -> str:
    """Build GET /api/v1/applications/<uuid>/deployments."""
    return f"{coolify_url.rstrip('/')}/api/v1/applications/{app_uuid}/deployments"


def build_docker_ps_command(app_id: str, use_local_vm: bool = False) -> List[str]:
    """Build the Docker label discovery command. Does not invent names."""
    command = [
        "docker",
        "ps",
        "--filter",
        f"label=coolify.application.id={app_id}",
        "--format",
        "{{.Names}}\t{{.ID}}\t{{.Status}}\t{{.Labels}}",
    ]
    if use_local_vm:
        return ["devbox", "ssh", "--", *command]
    return command


def build_docker_inspect_command(container_name: str, use_local_vm: bool = False) -> List[str]:
    """Build the Docker inspect command for label extraction."""
    command = [
        "docker",
        "inspect",
        container_name,
        "--format",
        "{{json .Config.Labels}}",
    ]
    if use_local_vm:
        return ["devbox", "ssh", "--", *command]
    return command


def parse_label_blob(raw: str) -> Dict[str, str]:
    """Parse Docker label output from JSON, k=v CSV, or Go map format."""
    text = (raw or "").strip()
    if not text or text in {"<no value>", "map[]", "{}", "null"}:
        return {}

    if text.startswith("{") or text.startswith("["):
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, list) and loaded:
            loaded = loaded[0]
        if isinstance(loaded, Mapping):
            if "Config" in loaded and isinstance(loaded["Config"], Mapping):
                labels = loaded["Config"].get("Labels") or {}
                if isinstance(labels, Mapping):
                    return {str(key): str(value) for key, value in labels.items()}
            return {str(key): str(value) for key, value in loaded.items()}

    if text.startswith("map[") and text.endswith("]"):
        inner = text[4:-1].strip()
        if not inner:
            return {}
        labels: Dict[str, str] = {}
        for match in re.finditer(r"([^\s:]+):(\S*(?:\s(?![^\s:]+:)\S*)*)", inner):
            labels[match.group(1)] = match.group(2)
        return labels

    labels = {}
    for part in text.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key:
            labels[key] = value.strip()
    return labels


def _looks_like_container_name(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    if not candidate or validate_container_name(candidate) is not None:
        return False
    if candidate.lower() in {"finished", "success", "failed", "running", "healthy", "unknown"}:
        return False
    return True


def extract_container_name(payload: Any) -> Optional[str]:
    """Extract a container name from a Coolify-like JSON payload."""
    if payload is None:
        return None

    if isinstance(payload, list):
        for item in payload:
            found = extract_container_name(item)
            if found:
                return found
        return None

    if not isinstance(payload, Mapping):
        return None

    for key in _CONTAINER_NAME_KEYS:
        candidate = payload.get(key)
        if _looks_like_container_name(candidate):
            return str(candidate).strip()

    containers = payload.get("containers")
    if isinstance(containers, list):
        for item in containers:
            if isinstance(item, Mapping):
                for key in _CONTAINER_NAME_KEYS + ("name",):
                    candidate = item.get(key)
                    if _looks_like_container_name(candidate):
                        return str(candidate).strip()
            elif _looks_like_container_name(item):
                return str(item).strip()

    nested_keys = ("resource", "application", "data", "deployment")
    for key in nested_keys:
        found = extract_container_name(payload.get(key))
        if found:
            return found

    name = payload.get("name")
    if _looks_like_container_name(name) and (
        "container" in payload or payload.get("labels") or payload.get("Labels")
    ):
        return str(name).strip()

    return None


def extract_deployment_uuid(payload: Any) -> Optional[str]:
    """Extract a deployment uuid from a Coolify-like JSON payload."""
    if isinstance(payload, list):
        for item in payload:
            found = extract_deployment_uuid(item)
            if found:
                return found
        return None
    if not isinstance(payload, Mapping):
        return None
    for key in ("deployment_uuid", "deploymentUuid", "uuid", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip() and validate_app_uuid(value) is None:
            return value.strip()
    for key in ("data", "deployment", "latest"):
        found = extract_deployment_uuid(payload.get(key))
        if found:
            return found
    return None


def extract_labels(payload: Any) -> Dict[str, str]:
    """Extract label maps from a JSON payload if present."""
    if isinstance(payload, list):
        for item in payload:
            labels = extract_labels(item)
            if labels:
                return labels
        return {}
    if not isinstance(payload, Mapping):
        return {}
    for key in ("labels", "Labels"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            return {str(k): str(v) for k, v in value.items()}
        if isinstance(value, str):
            parsed = parse_label_blob(value)
            if parsed:
                return parsed
    for key in ("data", "deployment", "resource", "application", "Config"):
        labels = extract_labels(payload.get(key))
        if labels:
            return labels
    return {}


def _normalize_deployments_payload(payload: Any) -> List[Any]:
    if isinstance(payload, list):
        return list(payload)
    if isinstance(payload, Mapping):
        for key in ("data", "deployments"):
            value = payload.get(key)
            if isinstance(value, list):
                return list(value)
        return [payload]
    return []


def _preferred_deployments(items: Sequence[Any]) -> List[Any]:
    preferred: List[Any] = []
    rest: List[Any] = []
    for item in items:
        status = ""
        if isinstance(item, Mapping):
            status = str(item.get("status") or item.get("status_code") or "").lower()
        if status in {"finished", "success", "healthy", "running"}:
            preferred.append(item)
        else:
            rest.append(item)
    return preferred + rest


def discover_via_coolify_api(
    app_uuid: str,
    api_token: str,
    coolify_url: str,
    *,
    http_get: Optional[HttpGet] = None,
    timeout: int = DEFAULT_HTTP_TIMEOUT,
) -> DiscoveryResult:
    """Discover a container via Coolify deployments API.

    The token is used only as an Authorization header and is never copied
    into the result.
    """
    uuid_error = validate_app_uuid(app_uuid)
    if uuid_error:
        return _fail(DiscoveryMethod.COOLIFY_API, uuid_error)

    url_error = validate_coolify_url(coolify_url)
    if url_error:
        return _fail(DiscoveryMethod.COOLIFY_API, url_error)

    if not api_token or not str(api_token).strip():
        return _fail(DiscoveryMethod.COOLIFY_API, "api token is required")

    transport = http_get or default_http_get
    url = build_coolify_deployments_url(coolify_url, app_uuid)
    headers = {"Authorization": f"Bearer {str(api_token).strip()}"}

    try:
        status_code, body, transport_error = transport(url, headers, timeout)
    except Exception as exc:  # noqa: BLE001 — injected transports may raise
        return _fail(DiscoveryMethod.COOLIFY_API, f"transport error: {type(exc).__name__}")

    if transport_error and status_code == 0:
        return _fail(DiscoveryMethod.COOLIFY_API, redact_secrets(transport_error))

    if status_code != 200:
        detail = transport_error or f"HTTP {status_code}"
        return _fail(DiscoveryMethod.COOLIFY_API, redact_secrets(detail))

    try:
        payload = json.loads(body) if body else None
    except json.JSONDecodeError:
        return _fail(DiscoveryMethod.COOLIFY_API, "deployments response is not valid JSON")

    items = _preferred_deployments(_normalize_deployments_payload(payload))
    if not items:
        return _fail(DiscoveryMethod.COOLIFY_API, "no deployments found")

    for item in items:
        container_name = extract_container_name(item)
        if container_name:
            return _ok(
                DiscoveryMethod.COOLIFY_API,
                container_name,
                labels=extract_labels(item),
                deployment_uuid=extract_deployment_uuid(item),
            )

    # Last chance: payload as a whole (e.g. wrapper object).
    container_name = extract_container_name(payload)
    if container_name:
        return _ok(
            DiscoveryMethod.COOLIFY_API,
            container_name,
            labels=extract_labels(payload),
            deployment_uuid=extract_deployment_uuid(payload),
        )

    return _fail(
        DiscoveryMethod.COOLIFY_API,
        "deployments response did not include a container name",
        deployment_uuid=extract_deployment_uuid(payload),
        labels=extract_labels(payload),
    )


def _parse_docker_ps_line(line: str) -> Tuple[Optional[str], Dict[str, str], str]:
    parts = line.split("\t")
    name = parts[0].strip() if parts else ""
    status = parts[2].strip() if len(parts) > 2 else ""
    labels_raw = parts[3] if len(parts) > 3 else ""
    labels = parse_label_blob(labels_raw)
    if not name:
        return None, labels, status
    return name, labels, status


def discover_via_docker_labels(
    app_id: str,
    *,
    run_command: Optional[CommandRunner] = None,
    use_local_vm: bool = False,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> DiscoveryResult:
    """Discover a container via Docker labels in the local / the local VM VM."""
    uuid_error = validate_app_uuid(app_id)
    if uuid_error:
        return _fail(DiscoveryMethod.DOCKER_LABELS, uuid_error)

    transport = run_command or default_run_command
    command = build_docker_ps_command(app_id, use_local_vm=use_local_vm)

    try:
        exit_code, stdout, stderr = transport(command, timeout)
    except Exception as exc:  # noqa: BLE001
        return _fail(DiscoveryMethod.DOCKER_LABELS, f"transport error: {type(exc).__name__}")

    if exit_code != 0:
        detail = redact_secrets((stderr or stdout or "docker ps failed").strip())
        return _fail(DiscoveryMethod.DOCKER_LABELS, detail or "docker ps failed")

    matches: List[Tuple[str, Dict[str, str], str]] = []
    for raw_line in (stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        name, labels, status = _parse_docker_ps_line(line)
        if name:
            matches.append((name, labels, status))

    if not matches:
        return _fail(
            DiscoveryMethod.DOCKER_LABELS,
            f"no containers found for label coolify.application.id={app_id}",
        )

    name, labels, _status = matches[0]
    if "coolify.application.id" not in labels:
        labels = dict(labels)
        labels["coolify.application.id"] = app_id
    return _ok(DiscoveryMethod.DOCKER_LABELS, name, labels=labels)


def discover_via_docker_inspect(
    container_name: str,
    *,
    run_command: Optional[CommandRunner] = None,
    use_local_vm: bool = False,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> DiscoveryResult:
    """Inspect a discovered container and return its labels.

    This method never invents a container name. The name must already be known
    from a previous discovery step or an explicit caller argument.
    """
    name_error = validate_container_name(container_name)
    if name_error:
        return _fail(DiscoveryMethod.DOCKER_INSPECT, name_error)

    transport = run_command or default_run_command
    command = build_docker_inspect_command(container_name, use_local_vm=use_local_vm)

    try:
        exit_code, stdout, stderr = transport(command, timeout)
    except Exception as exc:  # noqa: BLE001
        return _fail(DiscoveryMethod.DOCKER_INSPECT, f"transport error: {type(exc).__name__}")

    if exit_code != 0:
        detail = redact_secrets((stderr or stdout or "docker inspect failed").strip())
        return _fail(DiscoveryMethod.DOCKER_INSPECT, detail or "docker inspect failed")

    labels = parse_label_blob(stdout or "")
    if not labels and (stdout or "").strip():
        # inspect may have returned a non-label payload; still accept the name
        # only if the command succeeded and the name is valid — but labels are
        # the point of this method, so missing parseable labels fail closed.
        return _fail(
            DiscoveryMethod.DOCKER_INSPECT,
            "inspect output did not contain parseable labels",
            container_name=container_name,
        )

    if not labels:
        return _fail(
            DiscoveryMethod.DOCKER_INSPECT,
            "inspect returned no labels",
            container_name=container_name,
        )

    return _ok(DiscoveryMethod.DOCKER_INSPECT, container_name, labels=labels)


def _fallback_order(primary: DiscoveryMethod) -> List[DiscoveryMethod]:
    remaining = [method for method in DiscoveryMethod if method is not primary]
    return [primary, *remaining]


def discover_container(
    app_uuid: str,
    method: DiscoveryMethod = DiscoveryMethod.COOLIFY_API,
    **kwargs: Any,
) -> DiscoveryResult:
    """Discover a container, optionally falling back across methods.

    Keyword arguments (all optional except those required by the chosen
    method):
    - api_token, coolify_url: Coolify API
    - app_id: Docker labels (defaults to app_uuid)
    - container_name: Docker inspect
    - http_get, run_command: injectable transports
    - use_local_vm: wrap Docker commands with ``devbox ssh --``
    - fallback: try remaining methods when the primary method fails (default True)
    - timeout: per-attempt timeout
    """
    if not isinstance(method, DiscoveryMethod):
        try:
            method = DiscoveryMethod(str(method))
        except ValueError:
            return _fail(
                DiscoveryMethod.COOLIFY_API,
                f"unknown discovery method {method!r}",
            )

    fallback = kwargs.get("fallback", True)
    methods = _fallback_order(method) if fallback else [method]
    errors: List[str] = []
    last_result: Optional[DiscoveryResult] = None

    for current in methods:
        if current is DiscoveryMethod.COOLIFY_API:
            result = discover_via_coolify_api(
                app_uuid,
                api_token=str(kwargs.get("api_token") or ""),
                coolify_url=str(kwargs.get("coolify_url") or DEFAULT_COOLIFY_URL),
                http_get=kwargs.get("http_get"),
                timeout=int(kwargs.get("timeout") or DEFAULT_HTTP_TIMEOUT),
            )
        elif current is DiscoveryMethod.DOCKER_LABELS:
            result = discover_via_docker_labels(
                str(kwargs.get("app_id") or app_uuid),
                run_command=kwargs.get("run_command"),
                use_local_vm=bool(kwargs.get("use_local_vm", False)),
                timeout=int(kwargs.get("timeout") or DEFAULT_COMMAND_TIMEOUT),
            )
        else:
            inspect_name = kwargs.get("container_name")
            result = discover_via_docker_inspect(
                str(inspect_name) if inspect_name is not None else "",
                run_command=kwargs.get("run_command"),
                use_local_vm=bool(kwargs.get("use_local_vm", False)),
                timeout=int(kwargs.get("timeout") or DEFAULT_COMMAND_TIMEOUT),
            )

        last_result = result
        if result.success:
            return result
        if result.error:
            errors.append(f"{current.value}: {result.error}")

    combined = "; ".join(errors) if errors else "all discovery methods failed"
    if last_result is None:
        return _fail(method, combined)
    return _fail(last_result.method, combined)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Real Resource Discovery — Coolify container discovery (portable core)"
    )
    parser.add_argument("--app-uuid", required=True, help="Coolify application UUID / id")
    parser.add_argument(
        "--method",
        default=DiscoveryMethod.COOLIFY_API.value,
        choices=[item.value for item in DiscoveryMethod],
        help="Primary discovery method",
    )
    parser.add_argument("--coolify-url", default=DEFAULT_COOLIFY_URL, help="Coolify base URL")
    parser.add_argument("--app-id", help="Docker label application id (defaults to --app-uuid)")
    parser.add_argument("--container-name", help="Known container name for docker inspect")
    parser.add_argument("--use-local-vm", action="store_true", help="Prefix Docker commands with devbox ssh --")
    parser.add_argument("--no-fallback", action="store_true", help="Do not try other methods on failure")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    token = os.environ.get("COOLIFY_API_TOKEN", "")

    result = discover_container(
        args.app_uuid,
        method=DiscoveryMethod(args.method),
        api_token=token,
        coolify_url=args.coolify_url,
        app_id=args.app_id or args.app_uuid,
        container_name=args.container_name,
        use_local_vm=args.use_local_vm,
        fallback=not args.no_fallback,
    )
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
