#!/usr/bin/env python3
"""Tests for kanban_app_build.py — Hermes Kanban adapter.

All Kanban interactions are injected fakes. No live hermes kanban calls.
Secret fixtures are obviously fake and never real credentials.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from kanban_app_build import (
    ALLOWED_TRANSITIONS,
    DEFAULT_SENIOR_REVIEW_PROMPT,
    SENIOR_REVIEW_ITEM_HEADINGS,
    SENIOR_REVIEW_MODEL,
    SENIOR_REVIEW_PROVIDER,
    SUPPORTED_PROFILES,
    TASK_ID_RE,
    KanbanTask,
    TaskStatus,
    add_comment,
    add_comment_result,
    advance_lifecycle,
    allowed_lifecycle_transition,
    attach_file,
    build_deployment_create_command,
    build_implementation_create_command,
    build_senior_review_create_command,
    command_has_assignee,
    command_has_parent,
    complete_task,
    contains_secret,
    create_deployment_task,
    create_implementation_task,
    create_senior_review_task,
    default_run_command,
    document_agy_job_start,
    format_workspace_flag,
    implementation_title,
    load_senior_review_prompt,
    main,
    parse_kanban_create_output,
    request_changes,
    request_review,
    validate_app_name,
    validate_file_path,
    validate_profile,
    validate_senior_review_prompt,
    validate_task_id,
    validate_workspace,
)


SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = (
    "select_profile.py",
    "scaffold_template.py",
    "git_gate.py",
    "review_checklist.py",
    "post_deploy_acceptance.py",
    "real_resource_discovery.py",
)
ADAPTER_ALLOWED_IMPORTS = {
    "argparse",
    "json",
    "os",
    "re",
    "subprocess",
    "sys",
    "dataclasses",
    "enum",
    "pathlib",
    "typing",
    "__future__",
}
FAKE_API_KEY_ASSIGNMENT = 'api_key="FAKESECRET_k1l2m3n4o5p6q7r8s9t0"'
WORKSPACE = "/tmp/agent-apps"


def _runner(mapping: Optional[Dict[str, Tuple[int, str, str]]] = None, default=(0, '{"id":"kb-1"}', "")):
    mapping = mapping or {}

    def _impl(command: Sequence[str], timeout: int):
        _impl.calls.append({"command": list(command), "timeout": timeout})
        sub = command[2] if len(command) >= 3 else ""
        if sub in mapping:
            return mapping[sub]
        return default

    _impl.calls = []
    return _impl


def _raise_runner(exc: Exception):
    def _impl(command: Sequence[str], timeout: int):
        _impl.calls.append({"command": list(command), "timeout": timeout})
        raise exc

    _impl.calls = []
    return _impl


def _write(path: Path, content: str) -> str:
    path.write_text(content, encoding="utf-8")
    return str(path)


class TestDataclassesAndValidators(unittest.TestCase):
    def test_task_to_dict_roundtrip_keys(self):
        task = KanbanTask(task_id="kb-1", title="Build: Demo (static-vite)", status="blocked")
        payload = task.to_dict()
        self.assertEqual(payload["task_id"], "kb-1")
        self.assertTrue(payload["success"])
        json.dumps(payload)

    def test_task_id_and_app_name_validation(self):
        self.assertIsNone(validate_task_id("kb-impl-1"))
        self.assertIsNotNone(validate_task_id(""))
        self.assertIsNotNone(validate_task_id("../escape"))
        self.assertIsNotNone(validate_task_id("has space"))
        self.assertIsNone(validate_app_name("Shopping List"))
        self.assertIsNotNone(validate_app_name(""))
        self.assertIsNotNone(validate_app_name("line\nbreak"))

    def test_workspace_and_profile_validation(self):
        self.assertIsNone(validate_workspace(WORKSPACE))
        self.assertIsNotNone(validate_workspace("relative/path"))
        self.assertIsNotNone(validate_workspace("/tmp/../etc"))
        self.assertIsNone(validate_profile("react-hono-sqlite"))
        self.assertIsNotNone(validate_profile("not-a-profile"))
        self.assertEqual(len(SUPPORTED_PROFILES), 6)

    def test_file_path_must_exist_and_reject_traversal(self):
        self.assertIsNotNone(validate_file_path("/no/such/file.md"))
        self.assertIsNotNone(validate_file_path("../secret.md", must_exist=False))
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
            handle.write("ok")
            name = handle.name
        try:
            self.assertIsNone(validate_file_path(name))
        finally:
            Path(name).unlink(missing_ok=True)

    def test_secret_detection_uses_fake_fixture_only(self):
        self.assertTrue(contains_secret(FAKE_API_KEY_ASSIGNMENT))
        self.assertFalse(contains_secret("agy-job started: job-id=demo, model=gemini-3.7-flash-high"))
        self.assertRegex("kb-ok", TASK_ID_RE)


class TestCommandBuilders(unittest.TestCase):
    def test_implementation_command_is_non_dispatchable(self):
        command = build_implementation_create_command(
            "Shopping List", "react-hono-sqlite", WORKSPACE, "brief"
        )
        self.assertFalse(command_has_assignee(command))
        self.assertIn("--initial-status", command)
        self.assertIn("blocked", command)
        self.assertEqual(command[0:3], ["hermes", "kanban", "create"])
        self.assertIn(implementation_title("Shopping List", "react-hono-sqlite"), command)
        self.assertIn(format_workspace_flag(WORKSPACE), command)
        self.assertNotIn("--attach", command)

    def test_senior_review_command_routes_qwen38_without_parent(self):
        command = build_senior_review_create_command(
            "Shopping List", "kb-impl-1", WORKSPACE, "# Senior Review Request"
        )
        self.assertFalse(command_has_parent(command))
        self.assertIn("--model", command)
        self.assertIn(SENIOR_REVIEW_MODEL, command)
        self.assertIn(SENIOR_REVIEW_PROVIDER, command)
        self.assertIn("default", command)
        body = command[command.index("--body") + 1]
        self.assertIn("kb-impl-1", body)

    def test_deployment_command_declares_parent(self):
        command = build_deployment_create_command("Shopping List", "kb-impl-1", WORKSPACE)
        self.assertTrue(command_has_parent(command))
        self.assertEqual(command[command.index("--parent") + 1], "kb-impl-1")
        self.assertIn("Git-Gate", command[command.index("--body") + 1])


class TestParseCreateOutput(unittest.TestCase):
    def test_parse_json_id(self):
        self.assertEqual(parse_kanban_create_output('{"id":"kb-json-1"}'), "kb-json-1")

    def test_parse_nested_and_plain(self):
        self.assertEqual(parse_kanban_create_output('{"task":{"id":"kb-nested"}}'), "kb-nested")
        self.assertEqual(parse_kanban_create_output("kb-plain-id\n"), "kb-plain-id")

    def test_parse_empty_or_garbage_fails_closed(self):
        self.assertIsNone(parse_kanban_create_output(""))
        self.assertIsNone(parse_kanban_create_output("{not-json"))
        self.assertIsNone(parse_kanban_create_output('{"title":"no-id"}'))


class TestCreateTasks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.brief = _write(self.root / "executor-brief.md", "ROLE\nAGY is the bounded executor.\n")
        self.app_brief = _write(self.root / "app-brief.json", '{"schema":1}')
        self.app_yaml = _write(self.root / "app.yaml", "slug: demo\n")
        prompt = DEFAULT_SENIOR_REVIEW_PROMPT.read_text(encoding="utf-8")
        self.prompt = _write(self.root / "senior-review-prompt.md", prompt)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_implementation_task_with_attachments(self):
        runner = _runner()
        task = create_implementation_task(
            "Shopping List",
            "react-hono-sqlite",
            self.brief,
            WORKSPACE,
            app_brief_path=self.app_brief,
            app_yaml_path=self.app_yaml,
            run_command=runner,
        )
        self.assertTrue(task.success)
        self.assertEqual(task.status, TaskStatus.BLOCKED.value)
        self.assertEqual(len(task.attachments), 3)
        subcommands = [call["command"][2] for call in runner.calls]
        self.assertEqual(subcommands[0], "create")
        self.assertEqual(subcommands.count("attach"), 3)
        self.assertNotIn("--assignee", runner.calls[0]["command"])

    def test_create_implementation_missing_brief_fails_closed(self):
        task = create_implementation_task(
            "Shopping List",
            "static-vite",
            str(self.root / "missing.md"),
            WORKSPACE,
            run_command=_runner(),
        )
        self.assertFalse(task.success)
        self.assertEqual(task.task_id, "")

    def test_create_implementation_kanban_failure(self):
        runner = _runner({"create": (1, "", "kanban exploded")})
        task = create_implementation_task(
            "Shopping List", "static-vite", self.brief, WORKSPACE, run_command=runner
        )
        self.assertFalse(task.success)
        self.assertIn("kanban create failed", task.error or "")

    def test_create_senior_review_task(self):
        runner = _runner(default=(0, '{"id":"kb-review-1"}', ""))
        task = create_senior_review_task(
            "Shopping List",
            "kb-impl-1",
            self.prompt,
            WORKSPACE,
            run_command=runner,
            extra_attachments=[self.app_brief],
        )
        self.assertTrue(task.success)
        self.assertEqual(task.task_id, "kb-review-1")
        self.assertEqual(task.model, SENIOR_REVIEW_MODEL)
        self.assertEqual(task.provider, SENIOR_REVIEW_PROVIDER)
        create_cmd = runner.calls[0]["command"]
        self.assertNotIn("--parent", create_cmd)
        self.assertIn("kb-impl-1", create_cmd[create_cmd.index("--body") + 1])

    def test_create_deployment_task_with_parent(self):
        runner = _runner(default=(0, '{"id":"kb-deploy-1"}', ""))
        task = create_deployment_task("Shopping List", "kb-impl-1", WORKSPACE, run_command=runner)
        self.assertTrue(task.success)
        self.assertEqual(task.parent_id, "kb-impl-1")
        self.assertIn("--parent", runner.calls[0]["command"])

    def test_unparseable_create_output_fails_closed(self):
        runner = _runner({"create": (0, "not-an-id!!!", "")})
        task = create_implementation_task(
            "Shopping List", "static-vite", self.brief, WORKSPACE, run_command=runner
        )
        self.assertFalse(task.success)
        self.assertIn("no parseable task id", task.error or "")


class TestLifecycleAndComments(unittest.TestCase):
    def test_lifecycle_blocked_to_complete(self):
        runner = _runner(default=(0, "", ""))
        first = advance_lifecycle("kb-1", "blocked", "in_progress", "starting", run_command=runner)
        self.assertTrue(first.success)
        review = advance_lifecycle(
            "kb-1", "in_progress", "review", "Implementation complete", run_command=runner
        )
        self.assertTrue(review.success)
        done = advance_lifecycle(
            "kb-1", "review", "complete", "Code acceptance passed", run_command=runner
        )
        self.assertTrue(done.success)
        subs = [call["command"][2] for call in runner.calls]
        self.assertIn("unblock", subs)
        self.assertIn("request-review", subs)
        self.assertIn("complete", subs)

    def test_invalid_lifecycle_transition_fails_closed(self):
        result = advance_lifecycle(
            "kb-1", "blocked", "complete", "nope", run_command=_runner()
        )
        self.assertFalse(result.success)
        self.assertIn("invalid lifecycle transition", result.error or "")
        self.assertTrue(allowed_lifecycle_transition("review", "in_progress"))
        self.assertIn("complete", ALLOWED_TRANSITIONS["review"])

    def test_request_review_uses_summary_flag(self):
        runner = _runner(default=(0, "", ""))
        self.assertTrue(request_review("kb-1", "Implementation complete, all tests passing", run_command=runner))
        command = runner.calls[0]["command"]
        self.assertEqual(command[2], "request-review")
        self.assertIn("--summary", command)
        self.assertNotIn("--body", command)

    def test_request_changes_uses_positional_reason(self):
        runner = _runner(default=(0, "", ""))
        self.assertTrue(request_changes("kb-1", "Fix findings: scope leak", run_command=runner))
        command = runner.calls[0]["command"]
        self.assertEqual(command[2], "request-changes")
        self.assertEqual(command[-1], "Fix findings: scope leak")
        self.assertNotIn("--body", command)

    def test_complete_and_comment_and_attach(self):
        runner = _runner(default=(0, "", ""))
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write("{}")
            name = handle.name
        try:
            self.assertTrue(attach_file("kb-1", name, run_command=runner))
        finally:
            Path(name).unlink(missing_ok=True)
        self.assertTrue(add_comment("kb-1", "reviewed git diff", run_command=runner))
        self.assertTrue(complete_task("kb-1", "Code acceptance passed", run_command=runner))
        subs = [call["command"][2] for call in runner.calls]
        self.assertEqual(subs, ["attach", "comment", "complete"])

    def test_secret_in_comment_fails_closed(self):
        runner = _runner()
        result = add_comment_result("kb-1", FAKE_API_KEY_ASSIGNMENT, run_command=runner)
        self.assertFalse(result.success)
        self.assertEqual(runner.calls, [])

    def test_document_agy_job_start_comment_format(self):
        runner = _runner(default=(0, "", ""))
        result = document_agy_job_start(
            "kb-1", "shopping-list-build-20260822", "gemini-3.7-flash-high", run_command=runner
        )
        self.assertTrue(result.success)
        comment = runner.calls[0]["command"][-1]
        self.assertIn("job-id=shopping-list-build-20260822", comment)
        self.assertIn("model=gemini-3.7-flash-high", comment)

    def test_kanban_action_failure_and_runner_exception_fail_closed(self):
        failing = _runner({"comment": (2, "", "boom"), "complete": (1, "", "nope")})
        self.assertFalse(add_comment("kb-1", "hello", run_command=failing))
        self.assertFalse(complete_task("kb-1", "done", run_command=failing))
        exploding = _raise_runner(RuntimeError("transport down"))
        self.assertFalse(request_review("kb-1", "please review", run_command=exploding))

    def test_empty_command_default_runner_fails_closed(self):
        code, _stdout, stderr = default_run_command([], 1)
        self.assertNotEqual(code, 0)
        self.assertIn("empty", stderr)


class TestSeniorReviewPrompt(unittest.TestCase):
    def test_prompt_contains_all_ten_items_a_to_j(self):
        text = DEFAULT_SENIOR_REVIEW_PROMPT.read_text(encoding="utf-8")
        self.assertEqual(len(SENIOR_REVIEW_ITEM_HEADINGS), 10)
        for heading in SENIOR_REVIEW_ITEM_HEADINGS:
            self.assertIn(heading, text)

    def test_prompt_output_schema_and_constraints(self):
        text = DEFAULT_SENIOR_REVIEW_PROMPT.read_text(encoding="utf-8")
        for fragment in (
            "verdict",
            "APPROVED",
            "CHANGES_REQUIRED",
            "ESCALATE_TO_CEDRIC",
            "mandatory_review_items",
            "security_assessment",
            "architecture_assessment",
            "Do NOT modify code",
            "Do NOT make deployment decisions",
            "Do NOT access secrets",
        ):
            self.assertIn(fragment, text)
        self.assertEqual(validate_senior_review_prompt(text), [])

    def test_incomplete_prompt_fails_validation(self):
        missing = validate_senior_review_prompt("# Senior Review Request\n")
        self.assertGreaterEqual(len(missing), 10)
        text, errors = load_senior_review_prompt(str(DEFAULT_SENIOR_REVIEW_PROMPT))
        self.assertTrue(text)
        self.assertEqual(errors, [])


class TestPortabilityAndCli(unittest.TestCase):
    def test_adapter_is_stdlib_only(self):
        tree = ast.parse((SCRIPT_DIR / "kanban_app_build.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], ADAPTER_ALLOWED_IMPORTS)
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertIn(node.module.split(".")[0], ADAPTER_ALLOWED_IMPORTS)

    def test_core_scripts_do_not_import_kanban_adapter(self):
        for name in CORE_SCRIPTS:
            source = (SCRIPT_DIR / name).read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotEqual(alias.name, "kanban_app_build")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    self.assertNotEqual(node.module, "kanban_app_build")
            self.assertNotIn("hermes kanban", source)

    def test_cli_print_implementation_command(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "kanban_app_build.py"),
                "print-implementation-command",
                "--app-name",
                "Demo",
                "--profile",
                "static-vite",
                "--workspace",
                WORKSPACE,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertNotIn("--assignee", payload["command"])

    def test_cli_validate_prompt_and_reject_relative_workspace(self):
        good = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "kanban_app_build.py"),
                "validate-prompt",
                str(DEFAULT_SENIOR_REVIEW_PROMPT),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(good.returncode, 0, good.stdout)
        bad = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "kanban_app_build.py"),
                "print-deployment-command",
                "--app-name",
                "Demo",
                "--parent-task-id",
                "kb-1",
                "--workspace",
                "relative",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(bad.returncode, 0)


class TestFullWorkflowIntegration(unittest.TestCase):
    def test_full_workflow_kanban_then_review_then_complete(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        brief = _write(root / "executor-brief.md", "ROLE\nbounded executor\n")
        prompt = _write(
            root / "senior-review-prompt.md",
            DEFAULT_SENIOR_REVIEW_PROMPT.read_text(encoding="utf-8"),
        )
        created = {"n": 0}

        def runner(command: Sequence[str], timeout: int):
            runner.calls.append(list(command))
            sub = command[2]
            if sub == "create":
                created["n"] += 1
                return 0, json.dumps({"id": f"kb-{created['n']}"}), ""
            return 0, "", ""

        runner.calls = []
        impl = create_implementation_task(
            "Shopping List", "react-hono-sqlite", brief, WORKSPACE, run_command=runner
        )
        self.assertTrue(impl.success)
        documented = document_agy_job_start(
            impl.task_id, "shopping-list-build", "gemini-3.7-flash-high", run_command=runner
        )
        self.assertTrue(documented.success)
        self.assertTrue(
            advance_lifecycle(
                impl.task_id, "blocked", "in_progress", "agy-job running", run_command=runner
            ).success
        )
        self.assertTrue(
            advance_lifecycle(
                impl.task_id,
                "in_progress",
                "review",
                "Implementation complete, all tests passing",
                run_command=runner,
            ).success
        )
        review = create_senior_review_task(
            "Shopping List", impl.task_id, prompt, WORKSPACE, run_command=runner
        )
        self.assertTrue(review.success)
        self.assertTrue(
            complete_task(impl.task_id, "Code acceptance passed", run_command=runner)
        )
        deploy = create_deployment_task(
            "Shopping List", impl.task_id, WORKSPACE, run_command=runner
        )
        self.assertTrue(deploy.success)
        self.assertEqual(deploy.parent_id, impl.task_id)
        tmp.cleanup()

    def test_error_propagation_blocks_lifecycle(self):
        runner = _runner({"create": (1, "", "unavailable")})
        tmp = tempfile.TemporaryDirectory()
        brief = _write(Path(tmp.name) / "brief.md", "ROLE\nexecutor\n")
        task = create_implementation_task(
            "Demo", "static-vite", brief, WORKSPACE, run_command=runner
        )
        tmp.cleanup()
        self.assertFalse(task.success)
        self.assertFalse(
            advance_lifecycle(task.task_id or "x", "blocked", "review", "skip", run_command=runner).success
        )


if __name__ == "__main__":
    unittest.main()
