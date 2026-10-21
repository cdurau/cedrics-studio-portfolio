"""Regression coverage for the portable Phase-11 app-brief contract."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = SCRIPT_DIR.parent / "templates"
BRIEF_PATH = TEMPLATES_DIR / "app-brief.json"
SCHEMA_PATH = TEMPLATES_DIR / "app-brief.schema.json"
SELECTOR_PATH = SCRIPT_DIR / "select_profile.py"

jsonschema = pytest.importorskip("jsonschema")


def _load(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def test_canonical_app_brief_matches_draft_2020_12_schema():
    schema = _load(SCHEMA_PATH)
    brief = _load(BRIEF_PATH)
    jsonschema.Draft202012Validator(schema).validate(brief)


def test_legacy_hermes_orchestration_fields_are_rejected():
    schema = _load(SCHEMA_PATH)
    brief = _load(BRIEF_PATH)
    brief["orchestration"] = {
        "kanban_task_id": None,
        "executor_routing": "agy-gemini-3.7-flash-high",
        "senior_review_required": False,
    }
    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(brief))
    assert errors, "legacy Hermes-specific orchestration fields must fail schema validation"


def test_cli_validate_runs_schema_and_semantic_validation():
    result = subprocess.run(
        [sys.executable, str(SELECTOR_PATH), "validate", str(BRIEF_PATH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "Draft 2020-12 schema and semantic validation" in result.stdout


def test_cli_validate_fails_for_schema_invalid_brief(tmp_path: Path):
    brief = _load(BRIEF_PATH)
    brief["orchestration"] = {"kanban_task_id": None}
    invalid_path = tmp_path / "app-brief.json"
    invalid_schema_path = tmp_path / "app-brief.schema.json"
    invalid_path.write_text(json.dumps(brief), encoding="utf-8")
    invalid_schema_path.write_text(SCHEMA_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SELECTOR_PATH), "validate", str(invalid_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Schema validation error" in result.stdout
