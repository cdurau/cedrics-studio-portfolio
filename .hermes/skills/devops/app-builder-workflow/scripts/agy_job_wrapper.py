#!/usr/bin/env python3
"""
agy_job_wrapper.py — agy-job Integration for App Builds.

Hermes-specific adapter for Portable App Builder Core.
stdlib-only. Invokes the existing agy-job runner; does not reimplement it.
Command execution is injectable so tests never start real jobs.
Fail-closed: invalid ids, models, workspaces, timeouts, and malformed
results all block.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple


CommandRunner = Callable[[Sequence[str], int], Tuple[int, str, str]]

FLASH_MODEL = "gemini-3.7-flash-high"
PRO_MODEL = "gemini-3.1-pro-high"
APPROVED_MODELS = frozenset({FLASH_MODEL, PRO_MODEL})

JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PRINT_TIMEOUT_RE = re.compile(r"^[1-9][0-9]*[smh]$")

DEFAULT_AGY_JOB_BIN = os.path.expanduser("~/.hermes/scripts/agy-job")
DEFAULT_COMMAND_TIMEOUT = 30
DEFAULT_START_TIMEOUT = 60
FLASH_SCOPE_FILE_LIMIT = 5

COMPLEXITY_FLASH = frozenset({"small", "clear", "simple", "low"})
COMPLEXITY_PRO = frozenset({"complex", "high", "multi-file", "architecture", "senior"})

REQUIRED_RESULT_KEYS = (
    "status",
    "summary",
    "files_changed",
    "tests",
    "warnings",
)

OPTIONAL_RESULT_KEYS = (
    "architecture_blockers",
    "recommended_follow_up",
)

TERMINAL_STATUSES = frozenset(
    {"SUCCESS", "FAILED", "ARCHITECTURE_BLOCKER", "STOPPED", "TIMEOUT"}
)
ACTIVE_STATUSES = frozenset({"STARTING", "RUNNING", "STOPPING"})

SUPPORTED_PROFILES: Tuple[str, ...] = (
    "static-vite",
    "react-hono-sqlite",
    "react-hono-postgres",
    "next-postgres",
    "python-fastapi",
    "python-service",
)

PROFILE_EXTRA_TESTS: Dict[str, Tuple[str, ...]] = {
    "static-vite": (
        "IPv4+IPv6 /healthz",
        "relative assets",
        "SPA fallback",
    ),
    "react-hono-sqlite": (
        "SQLite WAL mode",
        "persistence across container restart",
        "migration fail-closed",
    ),
    "react-hono-postgres": (
        "DB connection via internal URL",
        "migration fail-closed",
        "identity headers",
    ),
    "next-postgres": (
        "Root URL",
        "cookie",
        "redirect",
        "SSR render",
        "identity headers",
    ),
    "python-fastapi": (
        "/healthz",
        "migration (Alembic/SQLAlchemy)",
        "DB persistence",
    ),
    "python-service": (
        "/healthz",
        "API contract",
        "DB persistence",
    ),
}

EXECUTOR_BRIEF_SECTIONS: Tuple[str, ...] = (
    "ROLE",
    "GOAL",
    "WORKSPACE",
    "SOURCE TEMPLATE",
    "SCOPE",
    "CONSTRAINTS",
    "NON-GOALS",
    "TESTS",
    "OUTPUT",
)

SECRET_PATTERNS = [
    re.compile(
        r"(?i)(api[_-]?key|api[_-]?secret|password|passwd|pwd)\s*[:=]\s*['\"][^'\"]{8,}['\"]"
    ),
    re.compile(r"(?i)(token|secret|credential)\s*[:=]\s*['\"][^'\"]{16,}['\"]"),
    re.compile(r"(?i)-----BEGIN\s+(RSA|DSA|EC|OPENSSH)\s+PRIVATE\s+KEY-----"),
    re.compile(r"(?i)(AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}"),
    re.compile(r"(?i)ghp_[0-9a-zA-Z]{36}"),
    re.compile(r"(?i)sk-[0-9a-zA-Z]{48}"),
]


@dataclass(frozen=True)
class ModelSelection:
    """Result of executor model routing."""

    model: Optional[str]
    success: bool
    error: Optional[str] = None
    reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "success": self.success,
            "error": self.error,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class AgJobStartResult:
    """Result of attempting to start an agy-job."""

    job_id: str
    success: bool
    error: Optional[str] = None
    command: Tuple[str, ...] = ()
    stdout: str = ""

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "success": self.success,
            "error": self.error,
            "command": list(self.command),
        }


@dataclass(frozen=True)
class AgJobResult:
    """Parsed agy-job result (fail-closed)."""

    job_id: str
    status: str
    summary: str
    files_changed: List[str]
    tests: List[Dict[str, Any]]
    warnings: List[str]
    architecture_blockers: List[str]
    recommended_follow_up: List[str]
    success: bool = False
    error: Optional[str] = None
    command: Tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "summary": self.summary,
            "files_changed": list(self.files_changed),
            "tests": list(self.tests),
            "warnings": list(self.warnings),
            "architecture_blockers": list(self.architecture_blockers),
            "recommended_follow_up": list(self.recommended_follow_up),
            "success": self.success,
            "error": self.error,
            "command": list(self.command),
        }


def default_run_command(command: Sequence[str], timeout: int) -> Tuple[int, str, str]:
    """stdlib subprocess runner. Never uses a shell."""
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
        return 124, "", "timeout"
    except Exception as exc:  # noqa: BLE001 — fail-closed transport wrapper
        return 1, "", f"command failed: {type(exc).__name__}"


def resolve_agy_job_bin(agy_job_bin: Optional[str] = None) -> str:
    """Return the agy-job binary path. Does not execute it."""
    if agy_job_bin and str(agy_job_bin).strip():
        return str(agy_job_bin).strip()
    env_bin = os.environ.get("AGY_JOB_BIN", "").strip()
    if env_bin:
        return env_bin
    return DEFAULT_AGY_JOB_BIN


def validate_job_id(job_id: Optional[str]) -> Optional[str]:
    """Return an error if the job id is empty, too long, or has metacharacters."""
    if job_id is None or not str(job_id).strip():
        return "job id is required"
    value = str(job_id).strip()
    if not JOB_ID_RE.match(value):
        return "job id format is invalid"
    if ".." in value or "/" in value or "\\" in value:
        return "job id contains invalid characters"
    return None


def validate_model(model: Optional[str]) -> Optional[str]:
    """Return an error if the model is not an approved exact slug."""
    if model is None or not str(model).strip():
        return "model is required"
    if str(model).strip() not in APPROVED_MODELS:
        return f"model is not an approved executor slug: {model}"
    return None


def validate_workspace(
    workspace: Optional[str],
    *,
    must_exist: bool = True,
) -> Optional[str]:
    """Return an error if the workspace is not a safe absolute directory."""
    if workspace is None or not str(workspace).strip():
        return "workspace is required"
    value = str(workspace).strip()
    if not os.path.isabs(value):
        return "workspace must be an absolute path"
    if ".." in Path(value).parts or ".." in value:
        return "workspace must not contain path traversal"
    if any(ch in value for ch in ("\0", "\n", "\r")):
        return "workspace contains invalid characters"
    if must_exist:
        path = Path(value)
        if not path.exists():
            return "workspace does not exist"
        if not path.is_dir():
            return "workspace must be a directory"
    return None


def validate_prompt_file(prompt_path: Optional[str]) -> Optional[str]:
    """Return an error if the executor-brief path is unusable."""
    if prompt_path is None or not str(prompt_path).strip():
        return "executor brief path is required"
    value = str(prompt_path).strip()
    if ".." in Path(value).parts:
        return "executor brief path must not contain path traversal"
    path = Path(value)
    if not path.is_file():
        return f"executor brief does not exist: {value}"
    try:
        if path.stat().st_size <= 0:
            return "executor brief is empty"
    except OSError:
        return "executor brief is not readable"
    return None


def validate_slug(slug: Optional[str]) -> Optional[str]:
    """Return an error if the app slug is not a bounded identifier."""
    if slug is None or not str(slug).strip():
        return "slug is required"
    value = str(slug).strip()
    if len(value) < 2 or len(value) > 48:
        return "slug must be 2-48 characters"
    if not SLUG_RE.match(value):
        return "slug format is invalid"
    return None


def validate_profile(profile: Optional[str]) -> Optional[str]:
    if profile is None or not str(profile).strip():
        return "profile is required"
    if str(profile).strip() not in SUPPORTED_PROFILES:
        return f"unsupported profile: {profile}"
    return None


def validate_print_timeout(value: Optional[str]) -> Optional[str]:
    """Return an error if a print-timeout duration is malformed."""
    if value is None or not str(value).strip():
        return None
    if not PRINT_TIMEOUT_RE.match(str(value).strip()):
        return "timeout must look like 30s, 20m, or 1h"
    return None


def contains_secret(text: Optional[str]) -> bool:
    if not text:
        return False
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def parse_timeout_seconds(value: Optional[str]) -> Optional[int]:
    """Parse a duration like 30s / 20m / 1h into seconds. None if unset/invalid."""
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    if not PRINT_TIMEOUT_RE.match(text):
        return None
    amount = int(text[:-1])
    unit = text[-1]
    if unit == "s":
        return amount
    if unit == "m":
        return amount * 60
    if unit == "h":
        return amount * 3600
    return None


def is_timeout_exit(exit_code: int, stderr: str = "") -> bool:
    """True when the runner reports a timeout."""
    if exit_code == 124:
        return True
    return "timeout" in (stderr or "").lower()


def select_executor_model(
    scope_files: int,
    complexity: str,
    security_relevant: bool,
) -> ModelSelection:
    """Select Flash vs Pro from task characteristics.

    Flash: small, clear, non-security work.
    Pro: complex, multi-file, security-relevant, or unknown-safe-to-upgrade cases
    that still have a recognized complexity label. Unknown complexity fails closed.
    """
    if not isinstance(scope_files, int) or isinstance(scope_files, bool) or scope_files < 0:
        return ModelSelection(None, False, "scope_files must be a non-negative integer")
    if security_relevant:
        return ModelSelection(
            PRO_MODEL,
            True,
            reason="security-relevant work requires gemini-3.1-pro-high",
        )
    label = (complexity or "").strip().lower()
    if not label:
        return ModelSelection(None, False, "complexity is required")
    if label in COMPLEXITY_PRO or scope_files > FLASH_SCOPE_FILE_LIMIT:
        reason = (
            "scope exceeds flash file limit"
            if scope_files > FLASH_SCOPE_FILE_LIMIT
            else f"complexity '{label}' requires gemini-3.1-pro-high"
        )
        return ModelSelection(PRO_MODEL, True, reason=reason)
    if label in COMPLEXITY_FLASH:
        return ModelSelection(
            FLASH_MODEL,
            True,
            reason="small/clear non-security work uses gemini-3.7-flash-high",
        )
    return ModelSelection(None, False, f"unknown complexity: {complexity}")


def resolve_app_workspace(workspace: str, slug: str) -> str:
    """Map a runtime monorepo root (or app dir) to apps/<slug>."""
    root = workspace.rstrip("/")
    suffix = f"/apps/{slug}"
    if root.endswith(suffix):
        return root
    return f"{root}{suffix}"


def _tests_from_brief(app_brief: Mapping[str, Any], profile: str) -> List[str]:
    tests: List[str] = []
    app = app_brief.get("app") if isinstance(app_brief.get("app"), Mapping) else {}
    declared = app.get("tests") if isinstance(app.get("tests"), Mapping) else {}
    if declared.get("unit"):
        tests.append("unit tests (profile-local)")
    if declared.get("integration"):
        tests.append("integration tests (profile-local)")
    if declared.get("e2e_browser"):
        tests.append("e2e browser tests")
    if declared.get("health_check"):
        tests.append('/healthz returns {"ok": true}')
    if declared.get("persistence_check"):
        tests.append("persistence across container restart")
    tests.extend(PROFILE_EXTRA_TESTS.get(profile, ()))
    return tests


def generate_executor_brief(
    app_brief: Dict[str, Any],
    profile: str,
    slug: str,
    workspace: str,
) -> str:
    """Generate an executor brief from an app brief.

    Returns an empty string on validation failure (fail-closed).
    """
    generated, error = generate_executor_brief_result(app_brief, profile, slug, workspace)
    if error or generated is None:
        return ""
    return generated


def generate_executor_brief_result(
    app_brief: Dict[str, Any],
    profile: str,
    slug: str,
    workspace: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Generate an executor brief or return a validation error."""
    if not isinstance(app_brief, Mapping):
        return None, "app brief must be an object"
    for error in (
        validate_profile(profile),
        validate_slug(slug),
        validate_workspace(workspace, must_exist=False),
    ):
        if error:
            return None, error
    app = app_brief.get("app")
    if not isinstance(app, Mapping):
        return None, "app brief is missing app object"
    name = str(app.get("name") or "").strip()
    purpose = str(app.get("purpose") or "").strip()
    if not name and not purpose:
        return None, "app brief is missing app.name and app.purpose"
    non_goals = app.get("non_goals") or []
    if not isinstance(non_goals, list):
        return None, "app.non_goals must be a list"
    app_workspace = resolve_app_workspace(workspace, slug)
    tests = _tests_from_brief(app_brief, profile)
    if not tests:
        return None, "no tests could be derived from the app brief"
    goal = purpose or f"Implement {name} as a bounded {profile} app"
    non_goal_lines = [f"- {item}" for item in non_goals] or ["- (none declared in app brief)"]
    test_lines = [f"- {item}" for item in tests]
    brief = f"""ROLE
AGY is the bounded implementation executor. Hermes owns architecture and acceptance.

GOAL
{goal}

WORKSPACE
{app_workspace}

SOURCE TEMPLATE
Already scaffolded by orchestrator from templates/{profile}/. Executor works only inside apps/{slug}/.

SCOPE
- apps/{slug}/** only
- May install dependencies within apps/{slug}/
- May run local tests, builds, Docker builds within scope

CONSTRAINTS
- Phase 0-10 are COMPLETE and frozen
- Follow templates/CONTRACTS.md (container, health, routing, auth, migration, secrets)
- No secrets in Git, docs, or logs
- No commits, pushes, deployments
- No Coolify/Tailscale/Authentik/DNS changes
- No changes outside apps/{slug}/
- No architecture changes

NON-GOALS
{chr(10).join(non_goal_lines)}

TESTS
{chr(10).join(test_lines)}

OUTPUT
Only structured JSON matching agy-result-schema.json.
If architecture boundaries would need to change: ARCHITECTURE_BLOCKER.
"""
    if contains_secret(brief):
        return None, "generated brief appears to contain a secret"
    missing = [section for section in EXECUTOR_BRIEF_SECTIONS if f"{section}\n" not in brief]
    if missing:
        return None, "generated brief missing sections: " + ", ".join(missing)
    return brief, None


def executor_brief_has_required_sections(text: str) -> bool:
    """True when all ROLE/GOAL/.../OUTPUT headings are present."""
    if not text:
        return False
    return all(section in text.splitlines() for section in EXECUTOR_BRIEF_SECTIONS)


def build_start_command(
    job_id: str,
    model: str,
    workspace: str,
    executor_brief_path: str,
    agy_job_bin: Optional[str] = None,
) -> List[str]:
    """Build `agy-job start <job-id> <model> <workspace> <prompt-file>`."""
    return [
        resolve_agy_job_bin(agy_job_bin),
        "start",
        job_id,
        model,
        workspace,
        executor_brief_path,
    ]


def build_status_command(job_id: str, agy_job_bin: Optional[str] = None) -> List[str]:
    return [resolve_agy_job_bin(agy_job_bin), "status", job_id]


def build_result_command(job_id: str, agy_job_bin: Optional[str] = None) -> List[str]:
    return [resolve_agy_job_bin(agy_job_bin), "result", job_id]


def build_log_command(job_id: str, agy_job_bin: Optional[str] = None) -> List[str]:
    return [resolve_agy_job_bin(agy_job_bin), "log", job_id]


def build_stop_command(job_id: str, agy_job_bin: Optional[str] = None) -> List[str]:
    return [resolve_agy_job_bin(agy_job_bin), "stop", job_id]


def _run(
    command: Sequence[str],
    *,
    run_command: Optional[CommandRunner],
    timeout: int,
) -> Tuple[int, str, str]:
    runner = run_command or default_run_command
    try:
        return runner(command, timeout)
    except Exception as exc:  # noqa: BLE001 — injected transports may raise
        return 1, "", f"command failed: {type(exc).__name__}"


def _fail_start(job_id: str, error: str, command: Optional[Sequence[str]] = None) -> AgJobStartResult:
    return AgJobStartResult(
        job_id=job_id or "",
        success=False,
        error=error,
        command=tuple(command or ()),
    )


def _fail_result(
    job_id: str,
    error: str,
    *,
    status: str = "FAILED",
    command: Optional[Sequence[str]] = None,
) -> AgJobResult:
    return AgJobResult(
        job_id=job_id or "",
        status=status,
        summary=error,
        files_changed=[],
        tests=[],
        warnings=[],
        architecture_blockers=[],
        recommended_follow_up=[],
        success=False,
        error=error,
        command=tuple(command or ()),
    )


def start_agy_job(
    job_id: str,
    model: str,
    workspace: str,
    executor_brief_path: str,
    run_command: Optional[CommandRunner] = None,
    timeout: int = DEFAULT_START_TIMEOUT,
    agy_job_bin: Optional[str] = None,
) -> AgJobStartResult:
    """Start agy-job. Returns a structured result; success is False on any error."""
    for error in (
        validate_job_id(job_id),
        validate_model(model),
        validate_workspace(workspace, must_exist=True),
        validate_prompt_file(executor_brief_path),
    ):
        if error:
            return _fail_start(job_id or "", error)
            
    try:
        brief_content = Path(executor_brief_path).read_text(encoding="utf-8")
    except OSError as exc:
        return _fail_start(job_id, f"failed to read executor brief: {type(exc).__name__}")
        
    if contains_secret(brief_content):
        return _fail_start(job_id, "executor brief appears to contain a secret")
        
    if not executor_brief_has_required_sections(brief_content):
        return _fail_start(job_id, "executor brief is missing required sections")

    command = build_start_command(
        job_id, model, workspace, executor_brief_path, agy_job_bin=agy_job_bin
    )
    code, stdout, stderr = _run(command, run_command=run_command, timeout=timeout)
    if is_timeout_exit(code, stderr):
        return _fail_start(job_id, "agy-job start timed out", command)
    if code != 0:
        detail = (stderr or stdout or "agy-job start failed").strip()
        return _fail_start(job_id, f"agy-job start failed (exit {code}): {detail}", command)
    return AgJobStartResult(
        job_id=job_id,
        success=True,
        command=tuple(command),
        stdout=stdout,
    )


def get_agy_job_status(
    job_id: str,
    run_command: Optional[CommandRunner] = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
    agy_job_bin: Optional[str] = None,
    elapsed_seconds: Optional[int] = None,
    timeout_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """Get agy-job status as a dict. Fail-closed on parse/command errors."""
    error = validate_job_id(job_id)
    if error:
        return {"job_id": job_id or "", "status": "FAILED", "success": False, "error": error}
    command = build_status_command(job_id, agy_job_bin=agy_job_bin)
    code, stdout, stderr = _run(command, run_command=run_command, timeout=timeout)
    if is_timeout_exit(code, stderr):
        return {
            "job_id": job_id,
            "status": "TIMEOUT",
            "success": False,
            "error": "agy-job status timed out",
            "command": command,
        }
    if code != 0:
        detail = (stderr or stdout or "agy-job status failed").strip()
        return {
            "job_id": job_id,
            "status": "FAILED",
            "success": False,
            "error": f"agy-job status failed (exit {code}): {detail}",
            "command": command,
        }
    payload = _parse_json_object(stdout)
    if payload is None:
        return {
            "job_id": job_id,
            "status": "FAILED",
            "success": False,
            "error": "agy-job status returned unparseable output",
            "command": command,
        }
    status = str(payload.get("status") or "").upper()
    if not status:
        return {
            "job_id": job_id,
            "status": "FAILED",
            "success": False,
            "error": "agy-job status missing status field",
            "command": command,
        }
    if status not in TERMINAL_STATUSES and status not in ACTIVE_STATUSES:
        return {
            "job_id": job_id,
            "status": "FAILED",
            "success": False,
            "error": f"unknown status: {status}",
            "command": command,
        }
    timed_out = job_exceeded_timeout(status, elapsed_seconds, timeout_seconds)
    if timed_out:
        return {
            "job_id": job_id,
            "status": "TIMEOUT",
            "success": False,
            "error": "agy-job exceeded allotted runtime",
            "raw_status": status,
            "command": command,
        }
    return {
        "job_id": job_id,
        "status": status,
        "success": True,
        "error": None,
        "payload": payload,
        "command": command,
    }


def job_exceeded_timeout(
    status: str,
    elapsed_seconds: Optional[int],
    timeout_seconds: Optional[int],
) -> bool:
    """True when a still-active job has exceeded the allotted runtime."""
    if elapsed_seconds is None or timeout_seconds is None:
        return False
    if elapsed_seconds < 0 or timeout_seconds <= 0:
        return True
    if status.upper() in ACTIVE_STATUSES and elapsed_seconds > timeout_seconds:
        return True
    return False


def _parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def unwrap_result_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Accept either a bare result object or a runner wrapper with agy_result={}."""
    inner = payload.get("agy_result")
    if isinstance(inner, Mapping) and inner.get("status"):
        return dict(inner)
    return dict(payload)


def parse_agy_result(job_id: str, stdout: str) -> AgJobResult:
    """Parse agy-job result output. Malformed payloads become FAILED."""
    payload = _parse_json_object(stdout)
    if payload is None:
        return _fail_result(job_id, "agy-job result is missing or not JSON")
        
    if "runner_status" in payload:
        runner_status = str(payload.get("runner_status") or "").upper()
        if runner_status not in TERMINAL_STATUSES:
            return _fail_result(job_id, f"unknown runner status: {runner_status}")
        if runner_status != "SUCCESS":
            return _fail_result(job_id, f"runner status is {runner_status}")
            
        exit_code = payload.get("exit_code")
        if exit_code not in (0, "0"):
            return _fail_result(job_id, f"runner exit code is {exit_code}")
            
        if payload.get("agy_result") is None:
            return _fail_result(job_id, "missing agy_result")

    inner = unwrap_result_payload(payload)
    if not isinstance(inner, dict):
        return _fail_result(job_id, "agy-job result inner payload is not an object")

    missing = [key for key in REQUIRED_RESULT_KEYS if key not in inner]
    if missing:
        return _fail_result(
            job_id,
            "agy-job result missing required keys: " + ", ".join(missing),
        )
    status = str(inner.get("status") or "").upper()
    if not status:
        return _fail_result(job_id, "agy-job result has empty status")
    if status not in ("SUCCESS", "FAILED", "ARCHITECTURE_BLOCKER"):
        return _fail_result(job_id, f"unknown agy_result status: {status}")
    files_changed = inner.get("files_changed")
    tests = inner.get("tests")
    warnings = inner.get("warnings")
    if not isinstance(files_changed, list) or not isinstance(tests, list) or not isinstance(warnings, list):
        return _fail_result(job_id, "agy-job result fields have the wrong types")
    blockers = inner.get("architecture_blockers")
    follow_up = inner.get("recommended_follow_up")
    if blockers is None:
        blockers = []
    if follow_up is None:
        follow_up = []
    if not isinstance(blockers, list) or not isinstance(follow_up, list):
        return _fail_result(job_id, "agy-job result optional lists have the wrong types")
    if status == "SUCCESS":
        for test in tests:
            if not isinstance(test, Mapping):
                return _fail_result(job_id, "agy-job SUCCESS result has malformed tests")
            if test.get("result") != "passed" or test.get("exit_code") not in (0, "0"):
                return _fail_result(
                    job_id,
                    "agy-job SUCCESS result contains failed test evidence",
                )
    ok = status == "SUCCESS"
    return AgJobResult(
        job_id=job_id,
        status=status,
        summary=str(inner.get("summary") or ""),
        files_changed=[str(item) for item in files_changed],
        tests=[dict(item) if isinstance(item, Mapping) else {"raw": item} for item in tests],
        warnings=[str(item) for item in warnings],
        architecture_blockers=[str(item) for item in blockers],
        recommended_follow_up=[str(item) for item in follow_up],
        success=ok,
        error=None if ok else str(inner.get("summary") or status),
    )


def get_agy_job_result(
    job_id: str,
    run_command: Optional[CommandRunner] = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
    agy_job_bin: Optional[str] = None,
) -> AgJobResult:
    """Get and parse an agy-job result. Non-terminal or malformed output fails closed."""
    error = validate_job_id(job_id)
    if error:
        return _fail_result(job_id or "", error)
    command = build_result_command(job_id, agy_job_bin=agy_job_bin)
    code, stdout, stderr = _run(command, run_command=run_command, timeout=timeout)
    if is_timeout_exit(code, stderr):
        return _fail_result(job_id, "agy-job result timed out", status="TIMEOUT", command=command)
    if code != 0:
        detail = (stderr or stdout or "agy-job result failed").strip()
        return _fail_result(
            job_id,
            f"agy-job result failed (exit {code}): {detail}",
            command=command,
        )
    parsed = parse_agy_result(job_id, stdout)
    return AgJobResult(
        job_id=parsed.job_id,
        status=parsed.status,
        summary=parsed.summary,
        files_changed=list(parsed.files_changed),
        tests=list(parsed.tests),
        warnings=list(parsed.warnings),
        architecture_blockers=list(parsed.architecture_blockers),
        recommended_follow_up=list(parsed.recommended_follow_up),
        success=parsed.success,
        error=parsed.error,
        command=tuple(command),
    )


def get_agy_job_log(
    job_id: str,
    run_command: Optional[CommandRunner] = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
    agy_job_bin: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch agy-job logs. Fail-closed; does not invent content."""
    error = validate_job_id(job_id)
    if error:
        return {"job_id": job_id or "", "success": False, "error": error, "log": ""}
    command = build_log_command(job_id, agy_job_bin=agy_job_bin)
    code, stdout, stderr = _run(command, run_command=run_command, timeout=timeout)
    if is_timeout_exit(code, stderr):
        return {
            "job_id": job_id,
            "success": False,
            "error": "agy-job log timed out",
            "log": "",
            "command": command,
        }
    if code != 0:
        detail = (stderr or stdout or "agy-job log failed").strip()
        return {
            "job_id": job_id,
            "success": False,
            "error": f"agy-job log failed (exit {code}): {detail}",
            "log": "",
            "command": command,
        }
    return {"job_id": job_id, "success": True, "error": None, "log": stdout, "command": command}


def stop_agy_job(
    job_id: str,
    run_command: Optional[CommandRunner] = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
    agy_job_bin: Optional[str] = None,
) -> bool:
    """Stop an agy-job. Returns False on any error."""
    error = validate_job_id(job_id)
    if error:
        return False
    command = build_stop_command(job_id, agy_job_bin=agy_job_bin)
    code, _stdout, stderr = _run(command, run_command=run_command, timeout=timeout)
    if is_timeout_exit(code, stderr) or code != 0:
        return False
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="agy-job adapter for Portable App Builder (print-only; no live jobs)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("print-start-command", help="Print the agy-job start argv")
    start.add_argument("--job-id", required=True)
    start.add_argument("--model", required=True)
    start.add_argument("--workspace", required=True)
    start.add_argument("--prompt-file", required=True)
    start.add_argument("--agy-job-bin", default=None)

    select = sub.add_parser("select-model", help="Select Flash vs Pro")
    select.add_argument("--scope-files", type=int, required=True)
    select.add_argument("--complexity", required=True)
    select.add_argument("--security-relevant", action="store_true")

    brief = sub.add_parser("generate-brief", help="Generate an executor brief from JSON")
    brief.add_argument("--app-brief", required=True)
    brief.add_argument("--profile", required=True)
    brief.add_argument("--slug", required=True)
    brief.add_argument("--workspace", required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "print-start-command":
        for error in (
            validate_job_id(args.job_id),
            validate_model(args.model),
            validate_workspace(args.workspace, must_exist=False),
        ):
            if error:
                print(json.dumps({"ok": False, "error": error}), file=sys.stderr)
                return 1
        command = build_start_command(
            args.job_id,
            args.model,
            args.workspace,
            args.prompt_file,
            agy_job_bin=args.agy_job_bin,
        )
        print(json.dumps({"ok": True, "command": command}))
        return 0
    if args.command == "select-model":
        selection = select_executor_model(
            args.scope_files, args.complexity, args.security_relevant
        )
        print(json.dumps(selection.to_dict()))
        return 0 if selection.success else 1
    if args.command == "generate-brief":
        try:
            brief_payload = json.loads(Path(args.app_brief).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(json.dumps({"ok": False, "error": f"cannot read app brief: {type(exc).__name__}"}))
            return 1
        text, error = generate_executor_brief_result(
            brief_payload, args.profile, args.slug, args.workspace
        )
        if error or text is None:
            print(json.dumps({"ok": False, "error": error}))
            return 1
        print(text)
        return 0
    print(json.dumps({"ok": False, "error": "unknown command"}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
