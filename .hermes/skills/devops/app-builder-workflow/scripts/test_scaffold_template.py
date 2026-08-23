#!/usr/bin/env python3
"""
test_scaffold_template.py — Tests for Portable Core Template Scaffolding.

Covers:
- All 6 canonical profiles (success cases)
- Invalid profile
- Invalid slug/path
- Existing target (no overwrite)
- Scope escape / traversal
- Fail-closed behavior
- app.yaml generation
- No real production apps modified
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure scripts dir is on path
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from scaffold_template import (
    SUPPORTED_PROFILES,
    ScaffoldRequest,
    _copy_template,
    _generate_app_yaml,
    _should_exclude,
    scaffold,
    validate_slug,
    validate_profile,
    validate_paths,
)

# Real planning repo (read-only source for tests)
PLANNING_REPO = Path("/Users/owner/Projekte/lokales-ki-studio")


@pytest.fixture
def runtime_repo(tmp_path: Path) -> Path:
    """Create a minimal runtime repo structure with apps/ directory."""
    rr = tmp_path / "runtime-repo"
    rr.mkdir()
    (rr / "apps").mkdir()
    return rr


@pytest.fixture
def planning_repo(tmp_path: Path) -> Path:
    """Create a minimal planning repo with all 6 profile templates."""
    pr = tmp_path / "planning-repo"
    pr.mkdir()
    templates_dir = pr / "templates"
    templates_dir.mkdir()

    for profile in SUPPORTED_PROFILES:
        profile_dir = templates_dir / profile
        profile_dir.mkdir()
        # Create minimal template files
        (profile_dir / "README.md").write_text(f"# {profile}\nTest template.\n")
        (profile_dir / "Dockerfile").write_text(f"FROM scratch\n# {profile}\n")
        (profile_dir / "app-source.txt").write_text(f"source for {profile}")

        # Create a subdirectory with files
        sub = profile_dir / "source"
        sub.mkdir()
        (sub / "main.txt").write_text(f"main source for {profile}")

        # Create excluded directories (should NOT be copied)
        nm = profile_dir / "node_modules"
        nm.mkdir()
        (nm / "should-not-copy.txt").write_text("EXCLUDED")

        cache = profile_dir / "__pycache__"
        cache.mkdir()
        (cache / "should-not-copy.pyc").write_text("EXCLUDED")

        # Create excluded files
        (profile_dir / "should-not-copy.pyc").write_text("EXCLUDED")
        (profile_dir / ".DS_Store").write_text("EXCLUDED")

    return pr


# ============================================================
# Slug validation tests
# ============================================================


class TestSlugValidation:
    def test_valid_slug_simple(self):
        ok, err = validate_slug("my-app")
        assert ok is True
        assert err == ""

    def test_valid_slug_single_word(self):
        ok, err = validate_slug("shopping")
        assert ok is True

    def test_valid_slug_with_numbers(self):
        ok, err = validate_slug("app-v2")
        assert ok is True

    def test_valid_slug_min_length(self):
        ok, err = validate_slug("ab")
        assert ok is True

    def test_valid_slug_max_length(self):
        ok, err = validate_slug("a" * 48)
        assert ok is True

    def test_invalid_slug_empty(self):
        ok, err = validate_slug("")
        assert ok is False
        assert "empty" in err

    def test_invalid_slug_too_short(self):
        ok, err = validate_slug("a")
        assert ok is False
        assert "too short" in err

    def test_invalid_slug_too_long(self):
        ok, err = validate_slug("a" * 49)
        assert ok is False
        assert "too long" in err

    def test_invalid_slug_uppercase(self):
        ok, err = validate_slug("MyApp")
        assert ok is False

    def test_invalid_slug_special_chars(self):
        ok, err = validate_slug("my_app!")
        assert ok is False

    def test_invalid_slug_starts_with_hyphen(self):
        ok, err = validate_slug("-my-app")
        assert ok is False

    def test_invalid_slug_ends_with_hyphen(self):
        ok, err = validate_slug("my-app-")
        assert ok is False

    def test_invalid_slug_consecutive_hyphens(self):
        ok, err = validate_slug("my--app")
        assert ok is False
        assert "consecutive hyphens" in err

    def test_invalid_slug_underscore(self):
        ok, err = validate_slug("my_app")
        assert ok is False

    def test_invalid_slug_spaces(self):
        ok, err = validate_slug("my app")
        assert ok is False

    def test_invalid_slug_traversal(self):
        ok, err = validate_slug("../etc")
        assert ok is False


# ============================================================
# Profile validation tests
# ============================================================


class TestProfileValidation:
    def test_valid_all_profiles(self):
        for profile in SUPPORTED_PROFILES:
            ok, err = validate_profile(profile)
            assert ok is True, f"profile {profile} should be valid"

    def test_invalid_profile_unknown(self):
        ok, err = validate_profile("unknown-profile")
        assert ok is False
        assert "not supported" in err

    def test_invalid_profile_empty(self):
        ok, err = validate_profile("")
        assert ok is False

    def test_invalid_profile_case_sensitive(self):
        ok, err = validate_profile("Static-Vite")
        assert ok is False


# ============================================================
# Path validation tests
# ============================================================


class TestPathValidation:
    def test_valid_paths(self, planning_repo, runtime_repo):
        req = ScaffoldRequest(
            profile="static-vite",
            slug="test-app",
            planning_repo=planning_repo,
            runtime_repo=runtime_repo,
        )
        ok, errors = validate_paths(req)
        assert ok is True
        assert errors == []

    def test_invalid_planning_repo_missing(self, tmp_path, runtime_repo):
        req = ScaffoldRequest(
            profile="static-vite",
            slug="test-app",
            planning_repo=tmp_path / "nonexistent",
            runtime_repo=runtime_repo,
        )
        ok, errors = validate_paths(req)
        assert ok is False
        assert any("does not exist" in e for e in errors)

    def test_invalid_runtime_repo_missing(self, planning_repo, tmp_path):
        req = ScaffoldRequest(
            profile="static-vite",
            slug="test-app",
            planning_repo=planning_repo,
            runtime_repo=tmp_path / "nonexistent",
        )
        ok, errors = validate_paths(req)
        assert ok is False

    def test_invalid_apps_dir_missing(self, planning_repo, tmp_path):
        rr = tmp_path / "runtime"
        rr.mkdir()
        # No apps/ directory
        req = ScaffoldRequest(
            profile="static-vite",
            slug="test-app",
            planning_repo=planning_repo,
            runtime_repo=rr,
        )
        ok, errors = validate_paths(req)
        assert ok is False
        assert any("apps directory" in e for e in errors)

    def test_invalid_source_template_missing(self, planning_repo, runtime_repo):
        req = ScaffoldRequest(
            profile="nonexistent-profile",
            slug="test-app",
            planning_repo=planning_repo,
            runtime_repo=runtime_repo,
        )
        ok, errors = validate_paths(req)
        assert ok is False


# ============================================================
# Scope escape / traversal tests
# ============================================================


class TestScopeEscape:
    def test_target_must_be_inside_apps(self, planning_repo, runtime_repo):
        """Target directory must resolve inside runtime_repo/apps/."""
        # This is enforced by validate_paths — the target is always
        # runtime_repo/apps/<slug>, so it's always inside apps/
        # unless slug contains traversal characters (which validate_slug rejects)
        req = ScaffoldRequest(
            profile="static-vite",
            slug="normal-app",
            planning_repo=planning_repo,
            runtime_repo=runtime_repo,
        )
        ok, errors = validate_paths(req)
        assert ok is True
        # Verify target is inside apps/
        assert req.target_dir.resolve().is_relative_to(
            (runtime_repo / "apps").resolve()
        )

    def test_slug_traversal_rejected(self, planning_repo, runtime_repo):
        """Slugs with path traversal characters are rejected by validate_slug."""
        for bad_slug in ["../etc", "..", "foo/../../bar", "foo/bar"]:
            ok, err = validate_slug(bad_slug)
            assert ok is False, f"slug '{bad_slug}' should be rejected"

    def test_source_must_be_inside_templates(self, tmp_path, runtime_repo):
        """Source must be inside planning_repo/templates/."""
        pr = tmp_path / "planning"
        pr.mkdir()
        # No templates/ directory
        req = ScaffoldRequest(
            profile="static-vite",
            slug="test-app",
            planning_repo=pr,
            runtime_repo=runtime_repo,
        )
        ok, errors = validate_paths(req)
        assert ok is False


# ============================================================
# Exclusion tests
# ============================================================


class TestExclusions:
    def test_node_modules_excluded(self):
        assert _should_exclude(Path("node_modules")) is True

    def test_pycache_excluded(self):
        assert _should_exclude(Path("__pycache__")) is True

    def test_dist_excluded(self):
        assert _should_exclude(Path("dist")) is True

    def test_git_excluded(self):
        assert _should_exclude(Path(".git")) is True

    def test_next_excluded(self):
        assert _should_exclude(Path(".next")) is True

    def test_pyc_file_excluded(self):
        assert _should_exclude(Path("module.pyc")) is True

    def test_ds_store_excluded(self):
        assert _should_exclude(Path(".DS_Store")) is True

    def test_normal_file_not_excluded(self):
        assert _should_exclude(Path("main.py")) is False

    def test_normal_dir_not_excluded(self):
        assert _should_exclude(Path("source")) is False


# ============================================================
# Success cases — all 6 profiles
# ============================================================


class TestScaffoldSuccess:
    @pytest.mark.parametrize("profile", SUPPORTED_PROFILES)
    def test_scaffold_each_profile(self, planning_repo, runtime_repo, profile):
        """Each of the 6 profiles scaffolds successfully."""
        req = ScaffoldRequest(
            profile=profile,
            slug=f"test-{profile}",
            planning_repo=planning_repo,
            runtime_repo=runtime_repo,
        )
        result = scaffold(req)

        assert result["status"] == "SUCCESS"
        assert result["profile"] == profile
        assert result["slug"] == f"test-{profile}"
        assert result["app_yaml_generated"] is True
        assert result["files_count"] > 0

        # Verify target directory exists
        target = runtime_repo / "apps" / f"test-{profile}"
        assert target.is_dir()

        # Verify app.yaml was generated
        app_yaml = target / "app.yaml"
        assert app_yaml.is_file()
        content = app_yaml.read_text()
        assert f"slug: test-{profile}" in content
        assert f"profile: {profile}" in content
        assert "schema: 1" in content

        # Verify README was copied
        readme = target / "README.md"
        assert readme.is_file()

        # Verify Dockerfile was copied
        dockerfile = target / "Dockerfile"
        assert dockerfile.is_file()

        # Verify source subdirectory was copied
        source_dir = target / "source"
        assert source_dir.is_dir()
        assert (source_dir / "main.txt").is_file()

    @pytest.mark.parametrize("profile", SUPPORTED_PROFILES)
    def test_excluded_files_not_copied(self, planning_repo, runtime_repo, profile):
        """Excluded directories and files must not be copied."""
        slug = f"test-excl-{profile}"
        req = ScaffoldRequest(
            profile=profile,
            slug=slug,
            planning_repo=planning_repo,
            runtime_repo=runtime_repo,
        )
        result = scaffold(req)
        target = runtime_repo / "apps" / slug

        # node_modules must NOT exist
        assert not (target / "node_modules").exists()
        # __pycache__ must NOT exist
        assert not (target / "__pycache__").exists()
        # .pyc files must NOT exist
        pyc_files = list(target.rglob("*.pyc"))
        assert len(pyc_files) == 0
        # .DS_Store must NOT exist
        assert not (target / ".DS_Store").exists()

    def test_scaffold_with_real_planning_repo(self, runtime_repo):
        """Scaffold using the real planning repo as source (read-only)."""
        if not PLANNING_REPO.is_dir():
            pytest.skip("Real planning repo not available")

        req = ScaffoldRequest(
            profile="static-vite",
            slug="real-test-app",
            planning_repo=PLANNING_REPO,
            runtime_repo=runtime_repo,
        )
        result = scaffold(req)
        assert result["status"] == "SUCCESS"
        assert result["files_count"] > 0

        # Verify key files exist
        target = runtime_repo / "apps" / "real-test-app"
        assert (target / "app.yaml").is_file()
        assert (target / "Dockerfile").is_file()
        assert (target / "README.md").is_file()

        # node_modules must NOT be copied (large dir)
        assert not (target / "node_modules").exists()

    def test_app_yaml_content_per_profile(self, planning_repo, runtime_repo):
        """Verify app.yaml has correct database type per profile."""
        expected_db = {
            "static-vite": "none",
            "react-hono-sqlite": "sqlite",
            "react-hono-postgres": "postgres",
            "next-postgres": "postgres",
            "python-fastapi": "sqlite",
            "python-service": "sqlite",
        }
        for profile, db_type in expected_db.items():
            slug = f"yaml-test-{profile}"
            req = ScaffoldRequest(
                profile=profile,
                slug=slug,
                planning_repo=planning_repo,
                runtime_repo=runtime_repo,
            )
            scaffold(req)
            app_yaml = runtime_repo / "apps" / slug / "app.yaml"
            content = app_yaml.read_text()
            assert f"type: {db_type}" in content, (
                f"profile {profile} should have db type {db_type}"
            )
            assert "mode: path" in content
            assert "forward_auth: true" in content
            assert "authentik-forward-auth" in content


# ============================================================
# Existing target tests
# ============================================================


class TestExistingTarget:
    def test_existing_nonempty_target_rejected(self, planning_repo, runtime_repo):
        """Scaffolding into a non-empty existing directory fails without allow_overwrite."""
        slug = "existing-app"
        target = runtime_repo / "apps" / slug
        target.mkdir()
        (target / "existing-file.txt").write_text("existing content")

        req = ScaffoldRequest(
            profile="static-vite",
            slug=slug,
            planning_repo=planning_repo,
            runtime_repo=runtime_repo,
            allow_overwrite=False,
        )
        with pytest.raises(ValueError, match="already exists"):
            scaffold(req)

    def test_existing_target_with_overwrite(self, planning_repo, runtime_repo):
        """Scaffolding with allow_overwrite=True succeeds."""
        slug = "overwrite-app"
        target = runtime_repo / "apps" / slug
        target.mkdir()
        (target / "existing-file.txt").write_text("existing content")

        req = ScaffoldRequest(
            profile="static-vite",
            slug=slug,
            planning_repo=planning_repo,
            runtime_repo=runtime_repo,
            allow_overwrite=True,
        )
        result = scaffold(req)
        assert result["status"] == "SUCCESS"

    def test_empty_existing_target_ok(self, planning_repo, runtime_repo):
        """Scaffolding into an empty existing directory succeeds."""
        slug = "empty-target-app"
        target = runtime_repo / "apps" / slug
        target.mkdir()

        req = ScaffoldRequest(
            profile="static-vite",
            slug=slug,
            planning_repo=planning_repo,
            runtime_repo=runtime_repo,
        )
        result = scaffold(req)
        assert result["status"] == "SUCCESS"


# ============================================================
# Fail-closed tests
# ============================================================


class TestFailClosed:
    def test_invalid_profile_raises(self, planning_repo, runtime_repo):
        req = ScaffoldRequest(
            profile="invalid-profile",
            slug="test-app",
            planning_repo=planning_repo,
            runtime_repo=runtime_repo,
        )
        with pytest.raises(ValueError, match="not supported"):
            scaffold(req)

    def test_invalid_slug_raises(self, planning_repo, runtime_repo):
        req = ScaffoldRequest(
            profile="static-vite",
            slug="INVALID",
            planning_repo=planning_repo,
            runtime_repo=runtime_repo,
        )
        with pytest.raises(ValueError, match="invalid"):
            scaffold(req)

    def test_missing_planning_repo_raises(self, tmp_path, runtime_repo):
        req = ScaffoldRequest(
            profile="static-vite",
            slug="test-app",
            planning_repo=tmp_path / "nonexistent",
            runtime_repo=runtime_repo,
        )
        with pytest.raises(ValueError, match="does not exist"):
            scaffold(req)

    def test_missing_runtime_repo_raises(self, planning_repo, tmp_path):
        req = ScaffoldRequest(
            profile="static-vite",
            slug="test-app",
            planning_repo=planning_repo,
            runtime_repo=tmp_path / "nonexistent",
        )
        with pytest.raises(ValueError, match="does not exist"):
            scaffold(req)


# ============================================================
# CLI tests
# ============================================================


class TestCLI:
    def test_cli_list_profiles(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "scaffold_template.py"), "list-profiles"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "profiles" in data
        assert set(data["profiles"]) == set(SUPPORTED_PROFILES)

    def test_cli_validate_slug_valid(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "scaffold_template.py"), "validate-slug", "my-app"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["valid"] is True

    def test_cli_validate_slug_invalid(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "scaffold_template.py"), "validate-slug", "INVALID"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        data = json.loads(result.stdout)
        assert data["valid"] is False

    def test_cli_scaffold_success(self, planning_repo, runtime_repo):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "scaffold_template.py"),
                "scaffold",
                "--profile", "static-vite",
                "--slug", "cli-test-app",
                "--planning-repo", str(planning_repo),
                "--runtime-repo", str(runtime_repo),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["status"] == "SUCCESS"
        assert data["profile"] == "static-vite"
        assert data["slug"] == "cli-test-app"

    def test_cli_scaffold_failure(self, planning_repo, runtime_repo):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "scaffold_template.py"),
                "scaffold",
                "--profile", "invalid-profile",
                "--slug", "test-app",
                "--planning-repo", str(planning_repo),
                "--runtime-repo", str(runtime_repo),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        data = json.loads(result.stdout)
        assert data["status"] == "FAILED"


# ============================================================
# app.yaml generation tests
# ============================================================


class TestAppYamlGeneration:
    def test_generate_app_yaml_static(self):
        content = _generate_app_yaml("my-app", "static-vite")
        assert "slug: my-app" in content
        assert "profile: static-vite" in content
        assert "type: none" in content
        assert "mode: path" in content

    def test_generate_app_yaml_sqlite(self):
        content = _generate_app_yaml("my-app", "react-hono-sqlite")
        assert "type: sqlite" in content

    def test_generate_app_yaml_postgres(self):
        content = _generate_app_yaml("my-app", "react-hono-postgres")
        assert "type: postgres" in content

    def test_generate_app_yaml_identity_headers(self):
        content = _generate_app_yaml("my-app", "static-vite")
        assert "X-authentik-username" in content
        assert "X-authentik-email" in content
        assert "X-authentik-name" in content
        assert "X-authentik-uid" in content
        assert "X-authentik-groups" in content

    def test_generate_app_yaml_schema_version(self):
        content = _generate_app_yaml("my-app", "static-vite")
        assert "schema: 1" in content


# ============================================================
# Portability contract tests
# ============================================================


class TestPortabilityContract:
    def test_no_agent_specific_imports(self):
        """scaffold_template.py must not import agent-specific modules."""
        source = (SCRIPT_DIR / "scaffold_template.py").read_text()
        forbidden = ["hermes", "kanban", "agy", "gemini", "qwen", "codex", "opencode"]
        for mod in forbidden:
            # Check for import statements (not comments)
            for line in source.split("\n"):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if f"import {mod}" in stripped.lower():
                    pytest.fail(f"scaffold_template.py imports agent-specific module: {mod}")

    def test_stdlib_only(self):
        """scaffold_template.py must use only stdlib modules."""
        source = (SCRIPT_DIR / "scaffold_template.py").read_text()
        # Extract import lines
        imports = []
        for line in source.split("\n"):
            stripped = line.strip()
            if stripped.startswith("import ") or stripped.startswith("from "):
                imports.append(stripped)

        # Allowed stdlib modules
        allowed = {"argparse", "json", "re", "shutil", "sys", "pathlib", "dataclasses", "typing", "__future__"}
        for imp in imports:
            if imp.startswith("from "):
                mod = imp.split()[1].split(".")[0]
            else:
                mod = imp.split()[1].split(".")[0]
            assert mod in allowed, f"Non-stdlib import: {imp}"

    def test_no_runtime_specific_logic(self):
        """scaffold_template.py must not contain runtime-specific agent logic."""
        source = (SCRIPT_DIR / "scaffold_template.py").read_text()
        forbidden_patterns = [
            "agy-job",
            "kanban create",
            "hermes kanban",
            "delegate_task",
        ]
        for pattern in forbidden_patterns:
            assert pattern not in source, f"Runtime-specific pattern found: {pattern}"
