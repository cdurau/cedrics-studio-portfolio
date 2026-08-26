#!/usr/bin/env python3
"""
kanban_app_build.py — Hermes Kanban Integration for App Builds.

Hermes-specific adapter for Portable App Builder Core.
stdlib-only. Command execution is injectable so tests never invoke Kanban.
Fail-closed: missing inputs, command failures, secrets, and unparsable
output all block.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple


CommandRunner = Callable[[Sequence[str], int], Tuple[int, str, str]]

HERMES_BIN = "hermes"
KANBAN = "kanban"

IMPLEMENTATION_MAX_RUNTIME = "60m"
SENIOR_REVIEW_MAX_RUNTIME = "30m"
DEPLOYMENT_MAX_RUNTIME = "30m"
DEFAULT_MAX_RETRIES = 1
DEFAULT_COMMAND_TIMEOUT = 30

SENIOR_REVIEW_MODEL = "qwen3.8-max"
SENIOR_REVIEW_PROVIDER = "alibaba"
SENIOR_REVIEW_ASSIGNEE = "default"
DEPLOYMENT_ASSIGNEE = "default"

SUPPORTED_PROFILES: Tuple[str, ...] = (
    "static-vite",
    "react-hono-sqlite",
    "react-hono-postgres",
    "next-postgres",
    "python-fastapi",
    "python-service",
)

TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
DEFAULT_SENIOR_REVIEW_PROMPT = Path(__file__).resolve().parent / "senior_review_prompt.md"

SENIOR_REVIEW_ITEM_HEADINGS: Tuple[str, ...] = (
    "A. Profile Selection Dimensions",
    "B. App-Brief vs Bounded app.yaml Separation",
    "C. Kanban / agy-job Responsibility Boundary",
    "D. Kanban Lifecycle / Acceptance",
    "E. New-App Git Change Set",
    "F. Code Acceptance vs Bounded Per-App Provisioning",
    "G. Control-Plane Remediation",
    "H. Real Coolify Resource Discovery",
    "I. Secret Incident Handling",
    "J. Mandatory Senior Review Gate",
)

SECRET_PATTERNS = [
    re.compile(
        r"(?i)(api[_-]?key|api[_-]?secret|password|passwd|pwd)\s*[:=]\s*['\"][^'\"]{8,}['\"]"
    ),
    re.compile(r"(?i)(token|secret|credential)\s*[:=]\s*['\"][^'\"]{16,}['\"]"),
    re.compile(r"(?i)-----BEGIN\s+(RSA|DSA|EC|OPENSSH)\s+PRIVATE\s+KEY-----"),
    re.compile(r"(?i)(AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}"),
    re.compile(r"(?i)ghp_[0-9a-zA-Z]{36}"),
    re.compile(r"(?i)github_pat_[0-9a-zA-Z_]{82}"),
    re.compile(r"(?i)sk-[0-9a-zA-Z]{48}"),
    re.compile(r"(?i)xox[baprs]-[0-9a-zA-Z-]{10,}"),
]


class TaskStatus(str, Enum):
    """Lifecycle statuses used by the app-build adapter."""

    BLOCKED = "blocked"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    COMPLETE = "complete"
    FAILED = "failed"


ALLOWED_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    TaskStatus.BLOCKED.value: (TaskStatus.IN_PROGRESS.value,),
    TaskStatus.IN_PROGRESS.value: (TaskStatus.REVIEW.value, TaskStatus.FAILED.value),
    TaskStatus.REVIEW.value: (
        TaskStatus.COMPLETE.value,
        TaskStatus.IN_PROGRESS.value,
        TaskStatus.FAILED.value,
    ),
}


@dataclass(frozen=True)
class KanbanTask:
    """A Kanban task created or inspected by this adapter."""

    task_id: str
    title: str
    status: str
    parent_id: Optional[str] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    assignee: Optional[str] = None
    attachments: Tuple[str, ...] = ()
    success: bool = True
    error: Optional[str] = None
    command: Tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "status": self.status,
            "parent_id": self.parent_id,
            "model": self.model,
            "provider": self.provider,
            "assignee": self.assignee,
            "attachments": list(self.attachments),
            "success": self.success,
            "error": self.error,
            "command": list(self.command),
        }


@dataclass(frozen=True)
class KanbanActionResult:
    """Result of a non-create Kanban action."""

    success: bool
    task_id: Optional[str] = None
    error: Optional[str] = None
    command: Tuple[str, ...] = ()
    stdout: str = ""

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "task_id": self.task_id,
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


def _fail_task(
    title: str,
    error: str,
    *,
    status: str = TaskStatus.FAILED.value,
    parent_id: Optional[str] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    assignee: Optional[str] = None,
    command: Optional[Sequence[str]] = None,
) -> KanbanTask:
    return KanbanTask(
        task_id="",
        title=title,
        status=status,
        parent_id=parent_id,
        model=model,
        provider=provider,
        assignee=assignee,
        success=False,
        error=error,
        command=tuple(command or ()),
    )


def _fail_action(
    error: str,
    *,
    task_id: Optional[str] = None,
    command: Optional[Sequence[str]] = None,
) -> KanbanActionResult:
    return KanbanActionResult(
        success=False,
        task_id=task_id,
        error=error,
        command=tuple(command or ()),
    )


def validate_app_name(app_name: Optional[str]) -> Optional[str]:
    """Return an error if the app name is unusable."""
    if app_name is None or not str(app_name).strip():
        return "app name is required"
    value = str(app_name).strip()
    if "\n" in value or "\r" in value:
        return "app name must be a single line"
    if len(value) > 200:
        return "app name is too long"
    return None


def validate_profile(profile: Optional[str]) -> Optional[str]:
    """Return an error if the profile is not a canonical app-builder profile."""
    if profile is None or not str(profile).strip():
        return "profile is required"
    if str(profile).strip() not in SUPPORTED_PROFILES:
        return f"unsupported profile: {profile}"
    return None


def validate_workspace(workspace: Optional[str]) -> Optional[str]:
    """Return an error if the workspace path is not a safe absolute directory path."""
    if workspace is None or not str(workspace).strip():
        return "workspace is required"
    value = str(workspace).strip()
    if not os.path.isabs(value):
        return "workspace must be an absolute path"
    if ".." in Path(value).parts or ".." in value:
        return "workspace must not contain path traversal"
    if any(ch in value for ch in ("\0", "\n", "\r")):
        return "workspace contains invalid characters"
    return None


def validate_task_id(task_id: Optional[str]) -> Optional[str]:
    """Return an error if the task id is empty or malformed."""
    if task_id is None or not str(task_id).strip():
        return "task id is required"
    value = str(task_id).strip()
    if not TASK_ID_RE.match(value):
        return "task id format is invalid"
    if ".." in value or "/" in value or "\\" in value:
        return "task id contains invalid characters"
    return None


def validate_file_path(file_path: Optional[str], *, must_exist: bool = True) -> Optional[str]:
    """Return an error if a file path is missing, traverses, or does not exist."""
    if file_path is None or not str(file_path).strip():
        return "file path is required"
    value = str(file_path).strip()
    if ".." in Path(value).parts:
        return "file path must not contain path traversal"
    path = Path(value)
    if must_exist and not path.is_file():
        return f"file does not exist: {value}"
    return None


def contains_secret(text: Optional[str]) -> bool:
    """True when text matches a high-confidence credential pattern."""
    if not text:
        return False
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def format_workspace_flag(workspace: str) -> str:
    """Kanban workspace flag value: dir:<absolute-path>."""
    return f"dir:{workspace.rstrip('/')}"


def implementation_title(app_name: str, profile: str) -> str:
    return f"Build: {app_name} ({profile})"


def senior_review_title(app_name: str) -> str:
    return f"Senior Review: {app_name}"


def deployment_title(app_name: str) -> str:
    return f"Deploy & Accept: {app_name}"


def allowed_lifecycle_transition(current: str, target: str) -> bool:
    """True when the status change is part of the app-build lifecycle."""
    return target in ALLOWED_TRANSITIONS.get(current, ())


def parse_kanban_create_output(stdout: str) -> Optional[str]:
    """Extract a task id from `hermes kanban create --json` output.

    Accepts a JSON object with id/task_id, a nested task object, or a bare id.
    Unclear output returns None (fail-closed).
    """
    text = (stdout or "").strip()
    if not text:
        return None
    payload: Optional[object] = None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        first_line = text.splitlines()[0].strip()
        if validate_task_id(first_line) is None:
            return first_line
        return None
    if isinstance(payload, dict):
        for key in ("id", "task_id", "taskId"):
            value = payload.get(key)
            if isinstance(value, str) and validate_task_id(value) is None:
                return value.strip()
        nested = payload.get("task")
        if isinstance(nested, dict):
            value = nested.get("id") or nested.get("task_id")
            if isinstance(value, str) and validate_task_id(value) is None:
                return value.strip()
    if isinstance(payload, str) and validate_task_id(payload) is None:
        return payload.strip()
    return None


def _read_text_file(file_path: str) -> Tuple[Optional[str], Optional[str]]:
    error = validate_file_path(file_path, must_exist=True)
    if error:
        return None, error
    try:
        text = Path(file_path).read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"cannot read file: {type(exc).__name__}"
    if not text.strip():
        return None, "file is empty"
    if contains_secret(text):
        return None, "file appears to contain a secret"
    return text, None


def build_implementation_create_command(
    app_name: str,
    profile: str,
    workspace: str,
    body: str,
) -> List[str]:
    """Build a non-dispatchable implementation-task create command.

    Intentionally omits --assignee. Orchestrator starts agy-job itself.
    """
    return [
        HERMES_BIN,
        KANBAN,
        "create",
        implementation_title(app_name, profile),
        "--body",
        body,
        "--initial-status",
        TaskStatus.BLOCKED.value,
        "--workspace",
        format_workspace_flag(workspace),
        "--max-runtime",
        IMPLEMENTATION_MAX_RUNTIME,
        "--max-retries",
        str(DEFAULT_MAX_RETRIES),
        "--json",
    ]


def build_senior_review_create_command(
    app_name: str,
    implementation_task_id: str,
    workspace: str,
    prompt_body: str,
) -> List[str]:
    """Build a senior-review create command.

    Must NOT use --parent: Kanban parents are true dependencies and would
    block the reviewer while the implementation task is still in review.
    """
    body = (
        f"Review implementation task {implementation_task_id}.\n\n"
        f"{prompt_body.rstrip()}\n"
    )
    return [
        HERMES_BIN,
        KANBAN,
        "create",
        senior_review_title(app_name),
        "--model",
        SENIOR_REVIEW_MODEL,
        "--provider",
        SENIOR_REVIEW_PROVIDER,
        "--assignee",
        SENIOR_REVIEW_ASSIGNEE,
        "--workspace",
        format_workspace_flag(workspace),
        "--body",
        body,
        "--max-runtime",
        SENIOR_REVIEW_MAX_RUNTIME,
        "--max-retries",
        str(DEFAULT_MAX_RETRIES),
        "--json",
    ]


def build_deployment_create_command(
    app_name: str,
    parent_task_id: str,
    workspace: str,
) -> List[str]:
    """Build a deployment task that cannot start until the parent is complete."""
    return [
        HERMES_BIN,
        KANBAN,
        "create",
        deployment_title(app_name),
        "--assignee",
        DEPLOYMENT_ASSIGNEE,
        "--parent",
        parent_task_id,
        "--body",
        "Git-Gate, Deployment, Post-Deployment Acceptance",
        "--workspace",
        format_workspace_flag(workspace),
        "--max-runtime",
        DEPLOYMENT_MAX_RUNTIME,
        "--json",
    ]


def command_has_assignee(command: Sequence[str]) -> bool:
    return "--assignee" in command


def command_has_parent(command: Sequence[str]) -> bool:
    return "--parent" in command


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


def _create_and_parse(
    command: Sequence[str],
    *,
    run_command: Optional[CommandRunner],
    timeout: int,
) -> Tuple[Optional[str], Optional[str], int, str]:
    code, stdout, stderr = _run(command, run_command=run_command, timeout=timeout)
    if code != 0:
        detail = (stderr or stdout or "kanban create failed").strip()
        return None, f"kanban create failed (exit {code}): {detail}", code, stdout
    task_id = parse_kanban_create_output(stdout)
    if not task_id:
        return None, "kanban create returned no parseable task id", code, stdout
    return task_id, None, code, stdout


def _attach_many(
    task_id: str,
    file_paths: Sequence[str],
    *,
    run_command: Optional[CommandRunner],
    timeout: int,
) -> Tuple[Tuple[str, ...], Optional[str]]:
    attached: List[str] = []
    for file_path in file_paths:
        result = attach_file(
            task_id,
            file_path,
            run_command=run_command,
            timeout=timeout,
        )
        if not result:
            return tuple(attached), f"failed to attach file: {file_path}"
        attached.append(file_path)
    return tuple(attached), None


def create_implementation_task(
    app_name: str,
    profile: str,
    executor_brief_path: str,
    workspace: str,
    app_brief_path: Optional[str] = None,
    app_yaml_path: Optional[str] = None,
    run_command: Optional[CommandRunner] = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> KanbanTask:
    """Create a non-dispatchable implementation task with attachments.

    Status starts as blocked. The orchestrator starts agy-job separately
    and documents the job via add_comment / document_agy_job_start.
    """
    title = implementation_title(app_name or "", profile or "")
    for error in (
        validate_app_name(app_name),
        validate_profile(profile),
        validate_workspace(workspace),
        validate_file_path(executor_brief_path, must_exist=True),
    ):
        if error:
            return _fail_task(title, error, status=TaskStatus.BLOCKED.value)
    body, read_error = _read_text_file(executor_brief_path)
    if read_error or body is None:
        return _fail_task(title, read_error or "executor brief unreadable", status=TaskStatus.BLOCKED.value)

    optional_paths = []
    for optional in (app_brief_path, app_yaml_path):
        if optional:
            optional_error = validate_file_path(optional, must_exist=True)
            if optional_error:
                return _fail_task(title, optional_error, status=TaskStatus.BLOCKED.value)
            optional_paths.append(optional)

    command = build_implementation_create_command(app_name, profile, workspace, body)
    if command_has_assignee(command):
        return _fail_task(
            title,
            "implementation task must not be dispatchable (--assignee forbidden)",
            status=TaskStatus.BLOCKED.value,
            command=command,
        )
    task_id, error, _code, _stdout = _create_and_parse(
        command, run_command=run_command, timeout=timeout
    )
    if error or not task_id:
        return _fail_task(
            title,
            error or "kanban create failed",
            status=TaskStatus.BLOCKED.value,
            command=command,
        )

    attachments = [executor_brief_path, *optional_paths]
    attached, attach_error = _attach_many(
        task_id, attachments, run_command=run_command, timeout=timeout
    )
    if attach_error:
        return KanbanTask(
            task_id=task_id,
            title=title,
            status=TaskStatus.BLOCKED.value,
            attachments=attached,
            success=False,
            error=attach_error,
            command=tuple(command),
        )
    return KanbanTask(
        task_id=task_id,
        title=title,
        status=TaskStatus.BLOCKED.value,
        attachments=attached,
        success=True,
        command=tuple(command),
    )


def create_senior_review_task(
    app_name: str,
    implementation_task_id: str,
    review_prompt_path: str,
    workspace: str,
    run_command: Optional[CommandRunner] = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
    extra_attachments: Optional[Sequence[str]] = None,
) -> KanbanTask:
    """Create a senior review task routed to Qwen3.8 Max.

    Does not set --parent. The implementation task id is referenced in the body.
    """
    title = senior_review_title(app_name or "")
    for error in (
        validate_app_name(app_name),
        validate_task_id(implementation_task_id),
        validate_workspace(workspace),
        validate_file_path(review_prompt_path, must_exist=True),
    ):
        if error:
            return _fail_task(
                title,
                error,
                model=SENIOR_REVIEW_MODEL,
                provider=SENIOR_REVIEW_PROVIDER,
                assignee=SENIOR_REVIEW_ASSIGNEE,
            )
    prompt_body, read_error = _read_text_file(review_prompt_path)
    if read_error or prompt_body is None:
        return _fail_task(
            title,
            read_error or "review prompt unreadable",
            model=SENIOR_REVIEW_MODEL,
            provider=SENIOR_REVIEW_PROVIDER,
            assignee=SENIOR_REVIEW_ASSIGNEE,
        )
    prompt_errors = validate_senior_review_prompt(prompt_body)
    if prompt_errors:
        return _fail_task(
            title,
            "review prompt is incomplete: " + "; ".join(prompt_errors),
            model=SENIOR_REVIEW_MODEL,
            provider=SENIOR_REVIEW_PROVIDER,
            assignee=SENIOR_REVIEW_ASSIGNEE,
        )

    command = build_senior_review_create_command(
        app_name, implementation_task_id, workspace, prompt_body
    )
    if command_has_parent(command):
        return _fail_task(
            title,
            "senior review task must not use --parent",
            model=SENIOR_REVIEW_MODEL,
            provider=SENIOR_REVIEW_PROVIDER,
            assignee=SENIOR_REVIEW_ASSIGNEE,
            command=command,
        )
    task_id, error, _code, _stdout = _create_and_parse(
        command, run_command=run_command, timeout=timeout
    )
    if error or not task_id:
        return _fail_task(
            title,
            error or "kanban create failed",
            model=SENIOR_REVIEW_MODEL,
            provider=SENIOR_REVIEW_PROVIDER,
            assignee=SENIOR_REVIEW_ASSIGNEE,
            command=command,
        )

    attachments = [review_prompt_path, *(extra_attachments or ())]
    attached, attach_error = _attach_many(
        task_id, attachments, run_command=run_command, timeout=timeout
    )
    if attach_error:
        return KanbanTask(
            task_id=task_id,
            title=title,
            status="ready",
            model=SENIOR_REVIEW_MODEL,
            provider=SENIOR_REVIEW_PROVIDER,
            assignee=SENIOR_REVIEW_ASSIGNEE,
            attachments=attached,
            success=False,
            error=attach_error,
            command=tuple(command),
        )
    return KanbanTask(
        task_id=task_id,
        title=title,
        status="ready",
        model=SENIOR_REVIEW_MODEL,
        provider=SENIOR_REVIEW_PROVIDER,
        assignee=SENIOR_REVIEW_ASSIGNEE,
        attachments=attached,
        success=True,
        command=tuple(command),
    )


def create_deployment_task(
    app_name: str,
    parent_task_id: str,
    workspace: str,
    run_command: Optional[CommandRunner] = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> KanbanTask:
    """Create a deployment task that depends on the completed implementation task."""
    title = deployment_title(app_name or "")
    for error in (
        validate_app_name(app_name),
        validate_task_id(parent_task_id),
        validate_workspace(workspace),
    ):
        if error:
            return _fail_task(title, error, parent_id=parent_task_id, assignee=DEPLOYMENT_ASSIGNEE)
    command = build_deployment_create_command(app_name, parent_task_id, workspace)
    if not command_has_parent(command):
        return _fail_task(
            title,
            "deployment task must declare --parent",
            parent_id=parent_task_id,
            assignee=DEPLOYMENT_ASSIGNEE,
            command=command,
        )
    task_id, error, _code, _stdout = _create_and_parse(
        command, run_command=run_command, timeout=timeout
    )
    if error or not task_id:
        return _fail_task(
            title,
            error or "kanban create failed",
            parent_id=parent_task_id,
            assignee=DEPLOYMENT_ASSIGNEE,
            command=command,
        )
    return KanbanTask(
        task_id=task_id,
        title=title,
        status="todo",
        parent_id=parent_task_id,
        assignee=DEPLOYMENT_ASSIGNEE,
        success=True,
        command=tuple(command),
    )


def attach_file(
    task_id: str,
    file_path: str,
    run_command: Optional[CommandRunner] = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> bool:
    """Attach a local file to a Kanban task. Fail-closed on any error."""
    result = attach_file_result(task_id, file_path, run_command=run_command, timeout=timeout)
    return result.success


def attach_file_result(
    task_id: str,
    file_path: str,
    run_command: Optional[CommandRunner] = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> KanbanActionResult:
    """Attach a local file and return a structured result."""
    for error in (validate_task_id(task_id), validate_file_path(file_path, must_exist=True)):
        if error:
            return _fail_action(error, task_id=task_id)
    command = [HERMES_BIN, KANBAN, "attach", task_id, file_path]
    code, stdout, stderr = _run(command, run_command=run_command, timeout=timeout)
    if code != 0:
        detail = (stderr or stdout or "attach failed").strip()
        return _fail_action(f"kanban attach failed (exit {code}): {detail}", task_id=task_id, command=command)
    return KanbanActionResult(success=True, task_id=task_id, command=tuple(command), stdout=stdout)


def add_comment(
    task_id: str,
    comment: str,
    run_command: Optional[CommandRunner] = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> bool:
    """Add a comment to a Kanban task. Secrets are rejected."""
    return add_comment_result(task_id, comment, run_command=run_command, timeout=timeout).success


def add_comment_result(
    task_id: str,
    comment: str,
    run_command: Optional[CommandRunner] = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> KanbanActionResult:
    error = validate_task_id(task_id)
    if error:
        return _fail_action(error, task_id=task_id)
    if comment is None or not str(comment).strip():
        return _fail_action("comment is required", task_id=task_id)
    text = str(comment).strip()
    if contains_secret(text):
        return _fail_action("comment appears to contain a secret", task_id=task_id)
    command = [HERMES_BIN, KANBAN, "comment", task_id, text]
    code, stdout, stderr = _run(command, run_command=run_command, timeout=timeout)
    if code != 0:
        detail = (stderr or stdout or "comment failed").strip()
        return _fail_action(f"kanban comment failed (exit {code}): {detail}", task_id=task_id, command=command)
    return KanbanActionResult(success=True, task_id=task_id, command=tuple(command), stdout=stdout)


def request_review(
    task_id: str,
    summary: str,
    run_command: Optional[CommandRunner] = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> bool:
    """Move a task into review. Uses --summary, not --body."""
    return request_review_result(task_id, summary, run_command=run_command, timeout=timeout).success


def request_review_result(
    task_id: str,
    summary: str,
    run_command: Optional[CommandRunner] = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> KanbanActionResult:
    error = validate_task_id(task_id)
    if error:
        return _fail_action(error, task_id=task_id)
    if summary is None or not str(summary).strip():
        return _fail_action("review summary is required", task_id=task_id)
    text = str(summary).strip()
    if contains_secret(text):
        return _fail_action("review summary appears to contain a secret", task_id=task_id)
    command = [HERMES_BIN, KANBAN, "request-review", task_id, "--summary", text]
    code, stdout, stderr = _run(command, run_command=run_command, timeout=timeout)
    if code != 0:
        detail = (stderr or stdout or "request-review failed").strip()
        return _fail_action(
            f"kanban request-review failed (exit {code}): {detail}",
            task_id=task_id,
            command=command,
        )
    return KanbanActionResult(success=True, task_id=task_id, command=tuple(command), stdout=stdout)


def request_changes(
    task_id: str,
    correction_brief: str,
    run_command: Optional[CommandRunner] = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> bool:
    """Request corrections. Reasons are positional, not --body."""
    return request_changes_result(task_id, correction_brief, run_command=run_command, timeout=timeout).success


def request_changes_result(
    task_id: str,
    correction_brief: str,
    run_command: Optional[CommandRunner] = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> KanbanActionResult:
    error = validate_task_id(task_id)
    if error:
        return _fail_action(error, task_id=task_id)
    if correction_brief is None or not str(correction_brief).strip():
        return _fail_action("correction brief is required", task_id=task_id)
    text = str(correction_brief).strip()
    if contains_secret(text):
        return _fail_action("correction brief appears to contain a secret", task_id=task_id)
    command = [HERMES_BIN, KANBAN, "request-changes", task_id, text]
    code, stdout, stderr = _run(command, run_command=run_command, timeout=timeout)
    if code != 0:
        detail = (stderr or stdout or "request-changes failed").strip()
        return _fail_action(
            f"kanban request-changes failed (exit {code}): {detail}",
            task_id=task_id,
            command=command,
        )
    return KanbanActionResult(success=True, task_id=task_id, command=tuple(command), stdout=stdout)


def complete_task(
    task_id: str,
    summary: str,
    run_command: Optional[CommandRunner] = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> bool:
    """Mark a task complete after acceptance. Uses --summary."""
    return complete_task_result(task_id, summary, run_command=run_command, timeout=timeout).success


def complete_task_result(
    task_id: str,
    summary: str,
    run_command: Optional[CommandRunner] = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> KanbanActionResult:
    error = validate_task_id(task_id)
    if error:
        return _fail_action(error, task_id=task_id)
    if summary is None or not str(summary).strip():
        return _fail_action("completion summary is required", task_id=task_id)
    text = str(summary).strip()
    if contains_secret(text):
        return _fail_action("completion summary appears to contain a secret", task_id=task_id)
    command = [HERMES_BIN, KANBAN, "complete", task_id, "--summary", text]
    code, stdout, stderr = _run(command, run_command=run_command, timeout=timeout)
    if code != 0:
        detail = (stderr or stdout or "complete failed").strip()
        return _fail_action(
            f"kanban complete failed (exit {code}): {detail}",
            task_id=task_id,
            command=command,
        )
    return KanbanActionResult(success=True, task_id=task_id, command=tuple(command), stdout=stdout)


def unblock_task(
    task_id: str,
    run_command: Optional[CommandRunner] = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> KanbanActionResult:
    """Unblock a blocked implementation task so work can be recorded."""
    error = validate_task_id(task_id)
    if error:
        return _fail_action(error, task_id=task_id)
    command = [HERMES_BIN, KANBAN, "unblock", task_id]
    code, stdout, stderr = _run(command, run_command=run_command, timeout=timeout)
    if code != 0:
        detail = (stderr or stdout or "unblock failed").strip()
        return _fail_action(
            f"kanban unblock failed (exit {code}): {detail}",
            task_id=task_id,
            command=command,
        )
    return KanbanActionResult(success=True, task_id=task_id, command=tuple(command), stdout=stdout)


def document_agy_job_start(
    task_id: str,
    job_id: str,
    model: str,
    run_command: Optional[CommandRunner] = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> KanbanActionResult:
    """Record an agy-job start as a Kanban comment (job id + model only)."""
    if not job_id or not str(job_id).strip():
        return _fail_action("job id is required", task_id=task_id)
    if not model or not str(model).strip():
        return _fail_action("model is required", task_id=task_id)
    comment = f"agy-job started: job-id={str(job_id).strip()}, model={str(model).strip()}"
    return add_comment_result(task_id, comment, run_command=run_command, timeout=timeout)


def advance_lifecycle(
    task_id: str,
    current_status: str,
    target_status: str,
    summary: str,
    run_command: Optional[CommandRunner] = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> KanbanActionResult:
    """Advance a task along the blocked → in_progress → review → complete path."""
    error = validate_task_id(task_id)
    if error:
        return _fail_action(error, task_id=task_id)
    current = (current_status or "").strip()
    target = (target_status or "").strip()
    if not allowed_lifecycle_transition(current, target):
        return _fail_action(
            f"invalid lifecycle transition: {current} -> {target}",
            task_id=task_id,
        )
    if target == TaskStatus.IN_PROGRESS.value and current == TaskStatus.BLOCKED.value:
        unblocked = unblock_task(task_id, run_command=run_command, timeout=timeout)
        if not unblocked.success:
            return unblocked
        return add_comment_result(
            task_id,
            "status=in_progress",
            run_command=run_command,
            timeout=timeout,
        )
    if target == TaskStatus.IN_PROGRESS.value and current == TaskStatus.REVIEW.value:
        return request_changes_result(
            task_id, summary, run_command=run_command, timeout=timeout
        )
    if target == TaskStatus.REVIEW.value:
        return request_review_result(
            task_id, summary, run_command=run_command, timeout=timeout
        )
    if target == TaskStatus.COMPLETE.value:
        return complete_task_result(
            task_id, summary, run_command=run_command, timeout=timeout
        )
    if target == TaskStatus.FAILED.value:
        return add_comment_result(
            task_id,
            f"status=failed: {summary}",
            run_command=run_command,
            timeout=timeout,
        )
    return _fail_action(f"unhandled lifecycle transition: {current} -> {target}", task_id=task_id)


def validate_senior_review_prompt(text: str) -> List[str]:
    """Return missing mandatory sections of the senior-review prompt."""
    missing: List[str] = []
    if not text or not text.strip():
        return ["prompt is empty"]
    for heading in SENIOR_REVIEW_ITEM_HEADINGS:
        if heading not in text:
            missing.append(heading)
    required_fragments = (
        "verdict",
        "APPROVED",
        "CHANGES_REQUIRED",
        "ESCALATE_TO_CEDRIC",
        "mandatory_review_items",
        "Do NOT modify code",
        "Do NOT make deployment decisions",
        "Do NOT access secrets",
    )
    for fragment in required_fragments:
        if fragment not in text:
            missing.append(fragment)
    return missing


def load_senior_review_prompt(path: Optional[str] = None) -> Tuple[Optional[str], List[str]]:
    """Load and validate the senior-review prompt template."""
    prompt_path = Path(path) if path else DEFAULT_SENIOR_REVIEW_PROMPT
    error = validate_file_path(str(prompt_path), must_exist=True)
    if error:
        return None, [error]
    try:
        text = prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [f"cannot read prompt: {type(exc).__name__}"]
    return text, validate_senior_review_prompt(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hermes Kanban adapter for Portable App Builder (print-only; no live dispatch)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    impl = sub.add_parser("print-implementation-command", help="Print the implementation create argv")
    impl.add_argument("--app-name", required=True)
    impl.add_argument("--profile", required=True)
    impl.add_argument("--workspace", required=True)
    impl.add_argument("--body", default="Executor brief placeholder")

    review = sub.add_parser("print-senior-review-command", help="Print the senior-review create argv")
    review.add_argument("--app-name", required=True)
    review.add_argument("--implementation-task-id", required=True)
    review.add_argument("--workspace", required=True)
    review.add_argument("--prompt-body", default="# Senior Review Request")

    deploy = sub.add_parser("print-deployment-command", help="Print the deployment create argv")
    deploy.add_argument("--app-name", required=True)
    deploy.add_argument("--parent-task-id", required=True)
    deploy.add_argument("--workspace", required=True)

    prompt = sub.add_parser("validate-prompt", help="Validate a senior-review prompt file")
    prompt.add_argument("path", nargs="?", default=str(DEFAULT_SENIOR_REVIEW_PROMPT))
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "print-implementation-command":
        for error in (
            validate_app_name(args.app_name),
            validate_profile(args.profile),
            validate_workspace(args.workspace),
        ):
            if error:
                print(json.dumps({"ok": False, "error": error}), file=sys.stderr)
                return 1
        command = build_implementation_create_command(
            args.app_name, args.profile, args.workspace, args.body
        )
        print(json.dumps({"ok": True, "command": command}))
        return 0
    if args.command == "print-senior-review-command":
        for error in (
            validate_app_name(args.app_name),
            validate_task_id(args.implementation_task_id),
            validate_workspace(args.workspace),
        ):
            if error:
                print(json.dumps({"ok": False, "error": error}), file=sys.stderr)
                return 1
        command = build_senior_review_create_command(
            args.app_name, args.implementation_task_id, args.workspace, args.prompt_body
        )
        print(json.dumps({"ok": True, "command": command, "has_parent": command_has_parent(command)}))
        return 0
    if args.command == "print-deployment-command":
        for error in (
            validate_app_name(args.app_name),
            validate_task_id(args.parent_task_id),
            validate_workspace(args.workspace),
        ):
            if error:
                print(json.dumps({"ok": False, "error": error}), file=sys.stderr)
                return 1
        command = build_deployment_create_command(
            args.app_name, args.parent_task_id, args.workspace
        )
        print(json.dumps({"ok": True, "command": command}))
        return 0
    if args.command == "validate-prompt":
        _text, missing = load_senior_review_prompt(args.path)
        payload = {"ok": not missing, "missing": missing, "path": args.path}
        print(json.dumps(payload))
        return 0 if not missing else 1
    print(json.dumps({"ok": False, "error": "unknown command"}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
