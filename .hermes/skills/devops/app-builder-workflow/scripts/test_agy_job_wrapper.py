#!/usr/bin/env python3

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import pytest

from agy_job_wrapper import (
    FLASH_MODEL,
    PRO_MODEL,
    AgJobResult,
    AgJobStartResult,
    ModelSelection,
    build_start_command,
    contains_secret,
    generate_executor_brief,
    get_agy_job_result,
    get_agy_job_status,
    select_executor_model,
    start_agy_job,
    validate_job_id,
    validate_model,
    validate_prompt_file,
    validate_workspace,
)

# --- Fixtures and Helpers ---

def create_mock_runner(
    code: int, stdout: str, stderr: str = ""
) -> Any:
    def mock_run(command: Sequence[str], timeout: int) -> Tuple[int, str, str]:
        if code == 124 or stderr == "timeout":
            # Simulate subprocess.TimeoutExpired behavior mapping in the wrapper
            return 124, "", "timeout"
        return code, stdout, stderr
    return mock_run


@pytest.fixture
def temp_workspace(tmp_path: Path) -> str:
    """Return a real absolute directory path."""
    return str(tmp_path.resolve())


@pytest.fixture
def temp_prompt_file(tmp_path: Path) -> str:
    """Return a real file path with some content."""
    p = tmp_path / "prompt.md"
    p.write_text("ROLE\nGOAL\nWORKSPACE\nSOURCE TEMPLATE\nSCOPE\nCONSTRAINTS\nNON-GOALS\nTESTS\nOUTPUT\n")
    return str(p.resolve())


@pytest.fixture
def invalid_prompt_file(tmp_path: Path) -> str:
    p = tmp_path / "invalid_prompt.md"
    p.write_text("Hello")
    return str(p.resolve())


@pytest.fixture
def secret_prompt_file(tmp_path: Path) -> str:
    p = tmp_path / "secret_prompt.md"
    p.write_text("ROLE\nGOAL\nWORKSPACE\nSOURCE TEMPLATE\nSCOPE\nCONSTRAINTS\nNON-GOALS\nTESTS\nOUTPUT\nAPI_KEY='1234567890'\n")
    return str(p.resolve())


# --- Test Model Selection ---

class TestModelSelection:
    def test_security_forces_pro(self):
        sel = select_executor_model(1, "small", True)
        assert sel.success is True
        assert sel.model == PRO_MODEL
        assert "security-relevant" in sel.reason

    def test_flash_for_small(self):
        sel = select_executor_model(2, "clear", False)
        assert sel.success is True
        assert sel.model == FLASH_MODEL

    def test_pro_for_complex(self):
        sel = select_executor_model(2, "complex", False)
        assert sel.success is True
        assert sel.model == PRO_MODEL

    def test_pro_for_large_scope(self):
        sel = select_executor_model(10, "small", False)
        assert sel.success is True
        assert sel.model == PRO_MODEL
        assert "exceeds flash file limit" in sel.reason

    def test_fails_closed_on_unknown(self):
        sel = select_executor_model(2, "unknown-label", False)
        assert sel.success is False
        assert sel.model is None


# --- Test Validation ---

class TestValidation:
    def test_validate_job_id(self):
        assert validate_job_id("valid-job-123") is None
        assert validate_job_id("invalid/job") is not None
        assert validate_job_id("..job") is not None
        assert validate_job_id("") is not None
        assert validate_job_id(None) is not None

    def test_validate_model(self):
        assert validate_model(FLASH_MODEL) is None
        assert validate_model(PRO_MODEL) is None
        assert validate_model("gpt-4") is not None

    def test_validate_workspace(self, temp_workspace: str):
        assert validate_workspace(temp_workspace) is None
        assert validate_workspace("relative/path") is not None
        assert validate_workspace(temp_workspace + "/../escape") is not None

    def test_validate_prompt_file(self, temp_prompt_file: str):
        assert validate_prompt_file(temp_prompt_file) is None
        assert validate_prompt_file("/does/not/exist.md") is not None


# --- Test Secret Detection ---

class TestSecretDetection:
    def test_contains_secret(self):
        assert contains_secret("API_KEY='1234567890'") is True
        assert contains_secret("token='12345678901234567890'") is True
        assert contains_secret("Normal text without secrets") is False


# --- Test Brief Generation ---

class TestBriefGeneration:
    def test_generate_brief_success(self, temp_workspace: str):
        app_brief = {
            "app": {
                "name": "TestApp",
                "purpose": "A test app",
                "non_goals": ["No DB"],
                "tests": {"health_check": True}
            }
        }
        brief = generate_executor_brief(app_brief, "static-vite", "test-app", temp_workspace)
        assert brief != ""
        assert "ROLE" in brief
        assert "OUTPUT" in brief
        assert "- No DB" in brief

    def test_generate_brief_secret_fails_closed(self, temp_workspace: str):
        app_brief = {
            "app": {
                "name": "TestApp",
                "purpose": "A test app",
                "non_goals": ["No DB"],
                "tests": {"health_check": True}
            }
        }
        # Inject secret via workspace name just to trigger the filter
        brief = generate_executor_brief(app_brief, "static-vite", "test-app", f"{temp_workspace}/API_KEY='1234567890'")
        assert brief == ""


# --- Test Start ---

class TestAgyJobStart:
    def test_start_success(self, temp_workspace: str, temp_prompt_file: str):
        runner = create_mock_runner(0, "started")
        res = start_agy_job("job1", FLASH_MODEL, temp_workspace, temp_prompt_file, run_command=runner)
        assert res.success is True
        assert res.job_id == "job1"
        assert "start" in res.command

    def test_start_timeout(self, temp_workspace: str, temp_prompt_file: str):
        runner = create_mock_runner(124, "", "timeout")
        res = start_agy_job("job1", FLASH_MODEL, temp_workspace, temp_prompt_file, run_command=runner)
        assert res.success is False
        assert "timed out" in res.error

    def test_start_invalid_args(self, temp_workspace: str, temp_prompt_file: str):
        res = start_agy_job("invalid/job", FLASH_MODEL, temp_workspace, temp_prompt_file)
        assert res.success is False
        assert "invalid" in res.error

    def test_start_invalid_brief_missing_sections(self, temp_workspace: str, invalid_prompt_file: str):
        res = start_agy_job("job1", FLASH_MODEL, temp_workspace, invalid_prompt_file)
        assert res.success is False
        assert "missing required sections" in res.error

    def test_start_brief_with_secret_fails_without_echoing(self, temp_workspace: str, secret_prompt_file: str):
        res = start_agy_job("job1", FLASH_MODEL, temp_workspace, secret_prompt_file)
        assert res.success is False
        assert "appears to contain a secret" in res.error
        assert "1234567890" not in res.error


# --- Test Status ---

class TestAgyJobStatus:
    def test_status_success(self):
        runner = create_mock_runner(0, '{"status": "RUNNING"}')
        res = get_agy_job_status("job1", run_command=runner)
        assert res["success"] is True
        assert res["status"] == "RUNNING"

    def test_status_exceeds_timeout(self):
        runner = create_mock_runner(0, '{"status": "RUNNING"}')
        res = get_agy_job_status("job1", run_command=runner, elapsed_seconds=4000, timeout_seconds=3600)
        assert res["success"] is False
        assert res["status"] == "TIMEOUT"

    def test_status_malformed_fails_closed(self):
        runner = create_mock_runner(0, 'not-json')
        res = get_agy_job_status("job1", run_command=runner)
        assert res["success"] is False
        assert res["status"] == "FAILED"

    def test_status_job_failed(self):
        # success=True means CLI query succeeded, not that the job succeeded
        runner = create_mock_runner(0, '{"status": "FAILED"}')
        res = get_agy_job_status("job1", run_command=runner)
        assert res["success"] is True  # CLI query succeeded
        assert res["status"] == "FAILED"  # but job failed

    def test_status_job_stopped(self):
        # success=True means CLI query succeeded, not that the job succeeded
        runner = create_mock_runner(0, '{"status": "STOPPED"}')
        res = get_agy_job_status("job1", run_command=runner)
        assert res["success"] is True  # CLI query succeeded
        assert res["status"] == "STOPPED"  # but job was stopped

    def test_status_cli_exit_nonzero(self):
        # CLI command itself failed (e.g., job not found)
        runner = create_mock_runner(2, '', 'job not found')
        res = get_agy_job_status("job1", run_command=runner)
        assert res["success"] is False
        assert res["status"] == "FAILED"
        assert "agy-job status failed" in res["error"]


# --- Test Result ---

class TestAgyJobResult:
    def test_result_success_clean(self):
        # Executor self-report does not constitute acceptance (Hermes accepts).
        # No commit/push/deploy/control-plane authority.
        # Kanban owns the queue; agy-job owns execution.
        output = {
            "job_id": "job1",
            "run_id": "run1",
            "runner_status": "SUCCESS",
            "exit_code": 0,
            "agy_result": {
                "status": "SUCCESS",
                "summary": "Done",
                "files_changed": ["a.txt"],
                "tests": [{"command": "test", "exit_code": 0, "result": "passed"}],
                "warnings": [],
                "architecture_blockers": [],
                "recommended_follow_up": []
            }
        }
        runner = create_mock_runner(0, json.dumps(output))
        res = get_agy_job_result("job1", run_command=runner)
        assert res.success is True
        assert res.status == "SUCCESS"

    def test_result_runner_failed_wins(self):
        output = {
            "runner_status": "FAILED",
            "exit_code": 1,
            "agy_result": {
                "status": "SUCCESS",
                "summary": "Done",
                "files_changed": [],
                "tests": [],
                "warnings": []
            }
        }
        runner = create_mock_runner(0, json.dumps(output))
        res = get_agy_job_result("job1", run_command=runner)
        assert res.success is False
        assert res.status == "FAILED"
        assert "runner status is FAILED" in res.error

    def test_result_nonzero_exit_code_fails(self):
        output = {
            "runner_status": "SUCCESS",
            "exit_code": 1,
            "agy_result": {
                "status": "SUCCESS",
                "summary": "Done",
                "files_changed": [],
                "tests": [],
                "warnings": []
            }
        }
        runner = create_mock_runner(0, json.dumps(output))
        res = get_agy_job_result("job1", run_command=runner)
        assert res.success is False
        assert res.status == "FAILED"
        assert "runner exit code is 1" in res.error

    def test_result_missing_agy_result_fails(self):
        output = {
            "runner_status": "SUCCESS",
            "exit_code": 0
        }
        runner = create_mock_runner(0, json.dumps(output))
        res = get_agy_job_result("job1", run_command=runner)
        assert res.success is False
        assert res.status == "FAILED"
        assert "missing agy_result" in res.error

    def test_result_malformed_json(self):
        runner = create_mock_runner(0, "NOT JSON")
        res = get_agy_job_result("job1", run_command=runner)
        assert res.success is False
        assert res.status == "FAILED"
        assert "not JSON" in res.error

    def test_result_inner_failed(self):
        output = {
            "runner_status": "SUCCESS",
            "exit_code": 0,
            "agy_result": {
                "status": "FAILED",
                "summary": "Done",
                "files_changed": [],
                "tests": [],
                "warnings": []
            }
        }
        runner = create_mock_runner(0, json.dumps(output))
        res = get_agy_job_result("job1", run_command=runner)
        assert res.success is False
        assert res.status == "FAILED"

    def test_result_architecture_blocker(self):
        output = {
            "runner_status": "SUCCESS",
            "exit_code": 0,
            "agy_result": {
                "status": "ARCHITECTURE_BLOCKER",
                "summary": "Need change",
                "files_changed": [],
                "tests": [],
                "warnings": [],
                "architecture_blockers": ["blocker1"],
                "recommended_follow_up": []
            }
        }
        runner = create_mock_runner(0, json.dumps(output))
        res = get_agy_job_result("job1", run_command=runner)
        assert res.success is False
        assert res.status == "ARCHITECTURE_BLOCKER"
        assert "blocker1" in res.architecture_blockers

    def test_result_runner_stopped(self):
        output = {
            "runner_status": "STOPPED",
            "exit_code": 0,
            "agy_result": None
        }
        runner = create_mock_runner(0, json.dumps(output))
        res = get_agy_job_result("job1", run_command=runner)
        assert res.success is False
        assert res.status == "FAILED"
        assert "runner status is STOPPED" in res.error

    def test_result_unknown_runner_status(self):
        output = {
            "runner_status": "UNKNOWN_XYZ",
            "exit_code": 0,
            "agy_result": None
        }
        runner = create_mock_runner(0, json.dumps(output))
        res = get_agy_job_result("job1", run_command=runner)
        assert res.success is False
        assert res.status == "FAILED"
        assert "unknown runner status: UNKNOWN_XYZ" in res.error

    def test_result_unknown_inner_status(self):
        output = {
            "runner_status": "SUCCESS",
            "exit_code": 0,
            "agy_result": {
                "status": "MAGIC_STATUS",
                "summary": "Done",
                "files_changed": [],
                "tests": [],
                "warnings": []
            }
        }
        runner = create_mock_runner(0, json.dumps(output))
        res = get_agy_job_result("job1", run_command=runner)
        assert res.success is False
        assert res.status == "FAILED"
        assert "unknown agy_result status: MAGIC_STATUS" in res.error

    def test_result_cli_exit_nonzero(self):
        # CLI command itself failed (e.g., job not found)
        runner = create_mock_runner(2, '', 'result is not available yet')
        res = get_agy_job_result("job1", run_command=runner)
        assert res.success is False
        assert res.status == "FAILED"
        assert "agy-job result failed" in res.error

    def test_result_success_with_blockers_rejected(self):
        # SUCCESS with non-empty architecture_blockers is contradictory
        output = {
            "runner_status": "SUCCESS",
            "exit_code": 0,
            "agy_result": {
                "status": "SUCCESS",
                "summary": "Done",
                "files_changed": ["a.txt"],
                "tests": [{"command": "test", "exit_code": 0, "result": "passed"}],
                "warnings": [],
                "architecture_blockers": ["blocker1"],
                "recommended_follow_up": []
            }
        }
        runner = create_mock_runner(0, json.dumps(output))
        res = get_agy_job_result("job1", run_command=runner)
        assert res.success is False
        assert res.status == "FAILED"
        assert "non-empty architecture_blockers" in res.error


# --- Test Command Builders (Array no Shell) ---

class TestCommandBuilders:
    def test_argument_array_construction(self):
        cmd = build_start_command("job1", FLASH_MODEL, "/ws", "/prompt.md")
        assert isinstance(cmd, list)
        assert "job1" in cmd
        assert "/ws" in cmd
        assert not any(" " in arg and not arg.startswith("/") for arg in cmd) # Check for injected shell spaces
