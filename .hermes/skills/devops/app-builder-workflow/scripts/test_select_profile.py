"""
test_select_profile.py — Comprehensive unit and validation tests for Phase 11 Slice 1.

Covers:
1. All 6 profiles with positive test cases (including next-postgres escalation).
2. Routing escalation cases (own_origin_needed, path_safe=false, ssr_rsc_needed).
3. Python priority order edge cases (python_needed evaluated before shared_state).
4. Fail-closed behavior on missing, invalid, or contradictory dimensions.
5. app-brief.json and app.yaml structure and schema validation.
6. Deterministic profile selection guarantee.
7. One-way app.yaml derivation from app-brief.json.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
import pytest
import yaml

from select_profile import (
    CANONICAL_DIMENSIONS,
    SUPPORTED_PROFILES,
    AppRequirements,
    ProfileSelectionResult,
    derive_app_manifest,
    evaluate_requirements,
    select_profile,
    validate_app_brief,
    validate_requirements_dict,
)

SCRIPTS_DIR = Path(__file__).parent
SKILL_DIR = SCRIPTS_DIR.parent
TEMPLATES_DIR = SKILL_DIR / "templates"
BRIEF_TEMPLATE_PATH = TEMPLATES_DIR / "app-brief.json"
MANIFEST_TEMPLATE_PATH = TEMPLATES_DIR / "app.yaml"


# =============================================================================
# Helper Fixtures
# =============================================================================

def make_reqs(**overrides) -> AppRequirements:
    """Helper to create AppRequirements with safe defaults for path-routed apps."""
    base = {
        "shared_state": False,
        "python_needed": False,
        "ssr_rsc_needed": False,
        "sqlite_sufficient": False,
        "path_safe": True,
        "own_origin_needed": False,
        "forward_auth_sufficient": True,
        "shadcn_needed": False,
        "backend_needed": False,
    }
    base.update(overrides)
    return AppRequirements(**base)


# =============================================================================
# 1. Behavior Coverage for All 6 Canonical Profiles
# =============================================================================

class TestProfileSelectionBehaviorCoverage:
    """Test behavior coverage for each canonical profile (F7: honest naming).
    
    Note: next-postgres is not directly selectable in Phase 10/11 (requires subdomain routing),
    so it correctly escalates. This tests the escalation path, not positive selection.
    """

    def test_static_vite_selected_when_no_shared_state_no_python(self):
        reqs = make_reqs(
            shared_state=False,
            python_needed=False,
            backend_needed=False,
        )
        assert select_profile(reqs) == "static-vite"

        result = evaluate_requirements(reqs)
        assert result.profile == "static-vite"
        assert not result.is_escalated
        assert result.escalation_reason is None
        assert "client-only static PWA" in result.rationale

    def test_react_hono_sqlite_selected_when_sqlite_sufficient(self):
        reqs = make_reqs(
            shared_state=True,
            python_needed=False,
            ssr_rsc_needed=False,
            sqlite_sufficient=True,
            backend_needed=True,
        )
        assert select_profile(reqs) == "react-hono-sqlite"

        result = evaluate_requirements(reqs)
        assert result.profile == "react-hono-sqlite"
        assert not result.is_escalated
        assert "SQLite sufficient" in result.rationale

    def test_react_hono_postgres_selected_when_postgres_justified(self):
        reqs = make_reqs(
            shared_state=True,
            python_needed=False,
            ssr_rsc_needed=False,
            sqlite_sufficient=False,
            backend_needed=True,
        )
        assert select_profile(reqs) == "react-hono-postgres"

        result = evaluate_requirements(reqs)
        assert result.profile == "react-hono-postgres"
        assert not result.is_escalated
        assert "PostgreSQL justified" in result.rationale

    def test_python_fastapi_selected_when_python_and_shared_state(self):
        reqs = make_reqs(
            shared_state=True,
            python_needed=True,
            sqlite_sufficient=True,
            backend_needed=True,
        )
        assert select_profile(reqs) == "python-fastapi"

        result = evaluate_requirements(reqs)
        assert result.profile == "python-fastapi"
        assert not result.is_escalated
        assert "FastAPI selected" in result.rationale

    def test_python_service_selected_when_python_and_no_shared_state(self):
        reqs = make_reqs(
            shared_state=False,
            python_needed=True,
            backend_needed=True,
        )
        assert select_profile(reqs) == "python-service"

        result = evaluate_requirements(reqs)
        assert result.profile == "python-service"
        assert not result.is_escalated
        assert "python-service selected" in result.rationale

    def test_next_postgres_escalates_due_to_subdomain_requirement(self):
        reqs = make_reqs(
            shared_state=True,
            python_needed=False,
            ssr_rsc_needed=True,
            sqlite_sufficient=False,
            backend_needed=True,
        )
        selected = select_profile(reqs)
        assert selected.startswith("ESCALATE_TO_CEDRIC:")
        assert "next-postgres requires subdomain routing" in selected

        result = evaluate_requirements(reqs)
        assert result.is_escalated
        assert result.profile is None
        assert "next-postgres" in result.escalation_reason


# =============================================================================
# 2. Escalation Cases
# =============================================================================

class TestEscalationCases:
    """Test all routing and platform escalation paths."""

    def test_escalate_when_own_origin_needed(self):
        reqs = make_reqs(
            own_origin_needed=True,
            shared_state=True,
            backend_needed=True,
        )
        selected = select_profile(reqs)
        assert selected.startswith("ESCALATE_TO_CEDRIC:")
        assert "own_origin_needed is true" in selected

        result = evaluate_requirements(reqs)
        assert result.is_escalated
        assert result.profile is None

    def test_escalate_when_path_safe_is_false(self):
        reqs = make_reqs(
            path_safe=False,
            shared_state=True,
            backend_needed=True,
        )
        selected = select_profile(reqs)
        assert selected.startswith("ESCALATE_TO_CEDRIC:")
        assert "path_safe is false" in selected

        result = evaluate_requirements(reqs)
        assert result.is_escalated
        assert result.profile is None

    def test_escalate_when_ssr_rsc_needed(self):
        reqs = make_reqs(
            ssr_rsc_needed=True,
            shared_state=True,
            backend_needed=True,
        )
        selected = select_profile(reqs)
        assert selected.startswith("ESCALATE_TO_CEDRIC:")
        assert "next-postgres requires subdomain routing" in selected


# =============================================================================
# 3. Python Ordering Edge Cases (P11-M1 Remediation)
# =============================================================================

class TestPythonOrderingEdgeCases:
    """Verify python_needed is evaluated before shared_state (P11-M1)."""

    def test_python_with_no_shared_state_yields_python_service_not_static_vite(self):
        # Even though shared_state is False, python_needed=True must select python-service, NOT static-vite
        reqs = make_reqs(
            python_needed=True,
            shared_state=False,
            backend_needed=True,
        )
        assert select_profile(reqs) == "python-service"

    def test_python_with_shared_state_yields_python_fastapi(self):
        reqs = make_reqs(
            python_needed=True,
            shared_state=True,
            backend_needed=True,
        )
        assert select_profile(reqs) == "python-fastapi"


# =============================================================================
# 4. Fail-Closed Validation & Contradiction Detection
# =============================================================================

class TestFailClosedValidation:
    """Test strict validation and fail-closed error handling."""

    def test_missing_dimension_raises_value_error(self):
        incomplete_dict = {
            "shared_state": True,
            "python_needed": False,
            # missing dimensions
        }
        with pytest.raises(ValueError, match="Missing canonical requirement dimension"):
            validate_requirements_dict(incomplete_dict)

    def test_non_boolean_dimension_raises_type_error(self):
        invalid_dict = {
            "shared_state": "yes",  # string instead of bool
            "python_needed": False,
            "ssr_rsc_needed": False,
            "sqlite_sufficient": True,
            "path_safe": True,
            "own_origin_needed": False,
            "forward_auth_sufficient": True,
            "shadcn_needed": False,
            "backend_needed": True,
        }
        with pytest.raises(TypeError, match="must be a boolean"):
            validate_requirements_dict(invalid_dict)

    def test_contradiction_shared_state_without_backend_raises_error(self):
        contradictory = {
            "shared_state": True,
            "python_needed": False,
            "ssr_rsc_needed": False,
            "sqlite_sufficient": True,
            "path_safe": True,
            "own_origin_needed": False,
            "forward_auth_sufficient": True,
            "shadcn_needed": False,
            "backend_needed": False,  # Contradiction!
        }
        with pytest.raises(ValueError, match="Contradictory requirements.*shared_state.*backend_needed"):
            validate_requirements_dict(contradictory)

    def test_contradiction_python_without_backend_raises_error(self):
        contradictory = {
            "shared_state": False,
            "python_needed": True,
            "ssr_rsc_needed": False,
            "sqlite_sufficient": False,
            "path_safe": True,
            "own_origin_needed": False,
            "forward_auth_sufficient": True,
            "shadcn_needed": False,
            "backend_needed": False,  # Contradiction!
        }
        with pytest.raises(ValueError, match="Contradictory requirements.*python_needed.*backend_needed"):
            validate_requirements_dict(contradictory)


# =============================================================================
# 5. Template & Schema Validation Tests
# =============================================================================

class TestTemplateValidation:
    """Validate app-brief.json and app.yaml template files."""

    def test_app_brief_template_exists_and_is_valid_json(self):
        assert BRIEF_TEMPLATE_PATH.exists(), f"{BRIEF_TEMPLATE_PATH} does not exist"
        with open(BRIEF_TEMPLATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert data.get("schema") == 1
        assert "app" in data
        assert "requirements" in data["app"]
        assert "profile_selection" in data
        assert "orchestration" in data
        assert "metadata" in data

    def test_all_nine_dimensions_present_in_app_brief_template(self):
        with open(BRIEF_TEMPLATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        reqs = data["app"]["requirements"]
        for dim in CANONICAL_DIMENSIONS:
            assert dim in reqs, f"Dimension '{dim}' missing in app-brief.json"
            assert isinstance(reqs[dim], bool), f"Dimension '{dim}' is not boolean in app-brief.json"

    def test_app_brief_passes_validator(self):
        with open(BRIEF_TEMPLATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        is_valid, errors = validate_app_brief(data)
        assert is_valid, f"Validation failed with errors: {errors}"
        assert len(errors) == 0

    def test_app_yaml_template_exists_and_is_valid_yaml(self):
        assert MANIFEST_TEMPLATE_PATH.exists(), f"{MANIFEST_TEMPLATE_PATH} does not exist"
        with open(MANIFEST_TEMPLATE_PATH, "r", encoding="utf-8") as f:
            manifest = yaml.safe_load(f)

        assert manifest["schema"] == 1
        assert "slug" in manifest
        assert manifest["profile"] in SUPPORTED_PROFILES
        assert manifest["routing"]["mode"] in ("path", "subdomain")
        assert manifest["database"]["type"] in ("sqlite", "postgres", "none")
        assert manifest["auth"]["forward_auth"] is True

    def test_one_way_derivation_from_app_brief(self):
        with open(BRIEF_TEMPLATE_PATH, "r", encoding="utf-8") as f:
            brief_data = json.load(f)

        derived = derive_app_manifest(brief_data)
        assert derived["schema"] == 1
        assert derived["slug"] == brief_data["app"]["slug"]
        assert derived["profile"] == "react-hono-sqlite"
        assert derived["database"]["type"] == "sqlite"
        assert derived["routing"]["mode"] == "path"
        assert derived["auth"]["forward_auth"] is True
        assert len(derived["auth"]["identity_headers"]) == 5


# =============================================================================
# 6. Determinism Guarantee Test
# =============================================================================

class TestDeterminism:
    """Verify that profile selection is strictly deterministic across repeated runs."""

    def test_determinism_across_runs(self):
        test_cases = [
            make_reqs(shared_state=False, python_needed=False, backend_needed=False),
            make_reqs(shared_state=True, sqlite_sufficient=True, backend_needed=True),
            make_reqs(shared_state=True, sqlite_sufficient=False, backend_needed=True),
            make_reqs(python_needed=True, shared_state=True, backend_needed=True),
            make_reqs(python_needed=True, shared_state=False, backend_needed=True),
            make_reqs(ssr_rsc_needed=True, shared_state=True, backend_needed=True),
            make_reqs(own_origin_needed=True, backend_needed=True),
            make_reqs(path_safe=False, backend_needed=True),
        ]

        for req in test_cases:
            first_run = select_profile(req)
            for _ in range(20):
                subsequent_run = select_profile(req)
                assert first_run == subsequent_run, "Profile selection is non-deterministic!"


# =============================================================================
# 7. Senior Review F1-F4 Regression Tests
# =============================================================================

class TestSeniorReviewF1F4Regression:
    """Regression tests for Senior Review F1-F4 findings (2026-08-23)."""

    def test_f1_ssr_with_shared_state_false_escalates(self):
        """F1: ssr_rsc_needed=true with shared_state=false must escalate, not select static-vite."""
        reqs = make_reqs(
            shared_state=False,
            ssr_rsc_needed=True,
            backend_needed=True,
        )
        result = select_profile(reqs)
        assert result.startswith("ESCALATE_TO_CEDRIC:")
        assert "ssr_rsc_needed" in result or "subdomain" in result

    def test_f2_backend_needed_true_with_shared_state_false_escalates(self):
        """F2: backend_needed=true with shared_state=false must escalate (stateless backend not supported)."""
        reqs = make_reqs(
            shared_state=False,
            backend_needed=True,
        )
        result = select_profile(reqs)
        assert result.startswith("ESCALATE_TO_CEDRIC:")
        assert "backend_needed" in result or "stateless" in result

    def test_f3_forward_auth_sufficient_false_escalates(self):
        """F3: forward_auth_sufficient=false must escalate (frozen ForwardAuth is only auth path)."""
        reqs = make_reqs(
            forward_auth_sufficient=False,
            shared_state=True,
            backend_needed=True,
        )
        result = select_profile(reqs)
        assert result.startswith("ESCALATE_TO_CEDRIC:")
        assert "forward_auth" in result or "auth" in result

    def test_f4_python_and_ssr_combination_escalates(self):
        """F4: python_needed=true with ssr_rsc_needed=true must escalate (no canonical profile)."""
        reqs = make_reqs(
            python_needed=True,
            ssr_rsc_needed=True,
            shared_state=True,
            backend_needed=True,
        )
        result = select_profile(reqs)
        assert result.startswith("ESCALATE_TO_CEDRIC:")
        assert "python" in result.lower() or "ssr" in result.lower() or "combination" in result.lower()

    def test_f5_apprequirements_instance_validation(self):
        """F5: AppRequirements instances must be validated (not just dicts)."""
        # This should raise ValueError for contradiction
        with pytest.raises(ValueError, match="shared_state.*backend_needed"):
            reqs = AppRequirements(
                shared_state=True,
                backend_needed=False,
                python_needed=False,
                ssr_rsc_needed=False,
                sqlite_sufficient=True,
                path_safe=True,
                own_origin_needed=False,
                forward_auth_sufficient=True,
                shadcn_needed=False,
            )
            evaluate_requirements(reqs)

    def test_f5_cli_contradictory_inputs_fail_closed(self):
        """F5: CLI evaluate with contradictory inputs must fail closed."""
        cmd = [
            sys.executable,
            str(SCRIPTS_DIR / "select_profile.py"),
            "evaluate",
            "--shared-state",
            "--no-backend-needed",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        assert result.returncode != 0
        assert "error" in result.stderr.lower() or "contradiction" in result.stderr.lower()

    def test_f6_profile_selection_consistency_mismatch_detected(self):
        """F6: profile_selection mismatch with computed profile must be detected."""
        brief_data = {
            "schema": 1,
            "app": {
                "name": "Test App",
                "slug": "test-app",
                "purpose": "Test",
                "requirements": {
                    "shared_state": True,
                    "python_needed": False,
                    "ssr_rsc_needed": False,
                    "sqlite_sufficient": True,
                    "path_safe": True,
                    "own_origin_needed": False,
                    "forward_auth_sufficient": True,
                    "shadcn_needed": False,
                    "backend_needed": True,
                },
            },
            "profile_selection": {
                "profile": "react-hono-postgres",  # Wrong! Should be react-hono-sqlite
                "rationale": "Wrong profile selected",
            },
            "orchestration": {
                "kanban_task_id": None,
                "executor_routing": "agy-gemini-3.7-flash-high",
                "senior_review_required": False,
            },
            "metadata": {
                "created_at": "2026-08-23T12:00:00Z",
                "created_by": "test",
            },
        }
        is_valid, errors = validate_app_brief(brief_data)
        assert not is_valid
        assert any("profile" in e.lower() and ("mismatch" in e.lower() or "does not match" in e.lower()) for e in errors)

    def test_f6_escalated_brief_with_profile_is_invalid(self):
        """F6: Escalated brief with non-null profile must be invalid."""
        brief_data = {
            "schema": 1,
            "app": {
                "name": "Test App",
                "slug": "test-app",
                "purpose": "Test",
                "requirements": {
                    "shared_state": False,
                    "python_needed": False,
                    "ssr_rsc_needed": True,  # Will escalate
                    "sqlite_sufficient": False,
                    "path_safe": True,
                    "own_origin_needed": False,
                    "forward_auth_sufficient": True,
                    "shadcn_needed": False,
                    "backend_needed": True,
                },
            },
            "profile_selection": {
                "profile": "next-postgres",  # Invalid! Should be null for escalated
                "rationale": "Escalated but profile set",
            },
            "orchestration": {
                "kanban_task_id": None,
                "executor_routing": "agy-gemini-3.7-flash-high",
                "senior_review_required": False,
            },
            "metadata": {
                "created_at": "2026-08-23T12:00:00Z",
                "created_by": "test",
            },
        }
        is_valid, errors = validate_app_brief(brief_data)
        assert not is_valid
        assert any("escalated" in e.lower() and "profile" in e.lower() for e in errors)


# =============================================================================
# 8. CLI Subcommand Tests
# =============================================================================

class TestCLICommands:
    """Test CLI execution of select_profile.py."""

    def test_cli_evaluate_command(self):
        cmd = [
            sys.executable,
            str(SCRIPTS_DIR / "select_profile.py"),
            "evaluate",
            "--shared-state",
            "--sqlite-sufficient",
            "--backend-needed",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        assert data["profile"] == "react-hono-sqlite"
        assert not data["is_escalated"]

    def test_cli_from_brief_command(self):
        cmd = [
            sys.executable,
            str(SCRIPTS_DIR / "select_profile.py"),
            "from-brief",
            str(BRIEF_TEMPLATE_PATH),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        assert data["profile"] == "react-hono-sqlite"
        assert not data["is_escalated"]

    def test_cli_validate_command(self):
        cmd = [
            sys.executable,
            str(SCRIPTS_DIR / "select_profile.py"),
            "validate",
            str(BRIEF_TEMPLATE_PATH),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        assert "VALID: app-brief.json is valid" in result.stdout

    def test_cli_derive_manifest_command(self, tmp_path):
        out_file = tmp_path / "derived_app.yaml"
        cmd = [
            sys.executable,
            str(SCRIPTS_DIR / "select_profile.py"),
            "derive-manifest",
            str(BRIEF_TEMPLATE_PATH),
            "-o",
            str(out_file),
        ]
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        assert out_file.exists()
        with open(out_file, "r", encoding="utf-8") as f:
            manifest = yaml.safe_load(f)
        assert manifest["profile"] == "react-hono-sqlite"
        assert manifest["database"]["type"] == "sqlite"
