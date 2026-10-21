from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
TEMPLATES_DIR = SKILL_DIR / "templates"


def _find_planning_repo(start: Path) -> Path:
    """Locate the planning repo root by marker, not by nesting depth.

    This used to be `SCRIPT_DIR.parents[4]`, which silently pointed outside the
    repository when the skill moved from `.hermes/skills/devops/` to `skills/`.
    Searching for the markers survives that, and fails loudly if they are gone.
    """
    for candidate in [start, *start.parents]:
        if (candidate / "templates").is_dir() and (candidate / ".git").exists():
            return candidate
    raise RuntimeError(f"planning repo root not found above {start}")


PLANNING_REPO = _find_planning_repo(SCRIPT_DIR)

sys.path.insert(0, str(SCRIPT_DIR))
from scaffold_template import (  # noqa: E402
    EXCLUDED_DIR_NAMES,
    SUPPORTED_PROFILES,
    ScaffoldRequest,
    _copy_template,
    _generate_app_yaml,
    _should_exclude,
    scaffold,
    validate_paths,
    validate_profile,
    validate_slug,
)


def make_planning(tmp_path: Path) -> Path:
    pr = tmp_path / "planning"
    templates = pr / "templates"
    templates.mkdir(parents=True)
    (templates / "CONTRACTS.md").write_text("contract", encoding="utf-8")
    for profile in SUPPORTED_PROFILES:
        root = templates / profile
        (root / "source").mkdir(parents=True)
        (root / "README.md").write_text(f"# {profile}\n", encoding="utf-8")
        (root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        (root / "source" / "main.txt").write_text(profile, encoding="utf-8")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "ignored.txt").write_text("ignored", encoding="utf-8")
        (root / "__pycache__").mkdir()
        (root / "__pycache__" / "ignored.pyc").write_bytes(b"x")
        (root / ".DS_Store").write_text("ignored", encoding="utf-8")
    return pr


@pytest.fixture
def planning_repo(tmp_path: Path) -> Path:
    return make_planning(tmp_path)


@pytest.fixture
def runtime_repo(tmp_path: Path) -> Path:
    rr = tmp_path / "runtime"
    (rr / "apps").mkdir(parents=True)
    return rr


@pytest.mark.parametrize("slug", ["ab", "shopping", "my-app", "app-v2", "a" * 48])
def test_valid_slugs(slug):
    assert validate_slug(slug) == (True, "")


@pytest.mark.parametrize(
    "slug",
    ["", "a", "A-app", "my_app", "my app", "-app", "app-", "app--two", "../etc", "foo/bar", "a" * 49],
)
def test_invalid_slugs(slug):
    ok, error = validate_slug(slug)
    assert not ok
    assert error


def test_all_six_profiles_are_canonical():
    assert SUPPORTED_PROFILES == (
        "static-vite",
        "react-hono-sqlite",
        "react-hono-postgres",
        "next-postgres",
        "python-fastapi",
        "python-service",
    )
    for profile in SUPPORTED_PROFILES:
        assert validate_profile(profile) == (True, "")


@pytest.mark.parametrize("profile", ["", "unknown", "Static-Vite", "../static-vite"])
def test_invalid_profile(profile):
    ok, error = validate_profile(profile)
    assert not ok
    assert "supported" in error


def test_validate_paths_happy(planning_repo, runtime_repo):
    ok, errors = validate_paths(ScaffoldRequest("static-vite", "test-app", planning_repo, runtime_repo))
    assert ok and not errors


def test_validate_paths_requires_distinct_repos(planning_repo):
    (planning_repo / "apps").mkdir()
    ok, errors = validate_paths(ScaffoldRequest("static-vite", "test-app", planning_repo, planning_repo))
    assert not ok
    assert "distinct" in errors[0]


def test_missing_planning_repo_fails(tmp_path, runtime_repo):
    ok, errors = validate_paths(ScaffoldRequest("static-vite", "test-app", tmp_path / "missing", runtime_repo))
    assert not ok and errors


def test_missing_runtime_repo_fails(planning_repo, tmp_path):
    ok, errors = validate_paths(ScaffoldRequest("static-vite", "test-app", planning_repo, tmp_path / "missing"))
    assert not ok and errors


def test_missing_apps_dir_fails(planning_repo, tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    ok, errors = validate_paths(ScaffoldRequest("static-vite", "test-app", planning_repo, runtime))
    assert not ok
    assert any("apps directory" in item for item in errors)


def test_source_profile_symlink_is_rejected(planning_repo, runtime_repo):
    source = planning_repo / "templates" / "static-vite"
    real = planning_repo / "templates" / "static-real"
    source.rename(real)
    source.symlink_to(real, target_is_directory=True)
    ok, errors = validate_paths(ScaffoldRequest("static-vite", "test-app", planning_repo, runtime_repo))
    assert not ok
    assert any("symlink" in item for item in errors)


def test_target_symlink_escape_is_rejected(planning_repo, runtime_repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (runtime_repo / "apps" / "test-app").symlink_to(outside, target_is_directory=True)
    ok, errors = validate_paths(ScaffoldRequest("static-vite", "test-app", planning_repo, runtime_repo))
    assert not ok
    assert any("escape" in item or "symlink" in item for item in errors)


@pytest.mark.parametrize("name", ["node_modules", "__pycache__", "dist", ".git", ".next"])
def test_excluded_dirs(name):
    assert name in EXCLUDED_DIR_NAMES
    assert _should_exclude(Path(name))


@pytest.mark.parametrize("name", ["mod.pyc", "mod.pyo", ".DS_Store", "Thumbs.db"])
def test_excluded_files(name):
    assert _should_exclude(Path(name))


def test_normal_path_not_excluded():
    assert not _should_exclude(Path("source"))
    assert not _should_exclude(Path("main.py"))


@pytest.mark.parametrize("profile", SUPPORTED_PROFILES)
def test_scaffold_success_all_profiles(planning_repo, runtime_repo, profile):
    slug = f"test-{profile}"
    result = scaffold(ScaffoldRequest(profile, slug, planning_repo, runtime_repo))
    target = runtime_repo / "apps" / slug
    assert result["status"] == "SUCCESS"
    assert result["profile"] == profile
    assert result["slug"] == slug
    assert target.is_dir()
    assert (target / "README.md").is_file()
    assert (target / "Dockerfile").is_file()
    assert (target / "source" / "main.txt").is_file()
    assert (target / "app.yaml").is_file()
    assert not (target / "node_modules").exists()
    assert not (target / "__pycache__").exists()
    assert not (target / ".DS_Store").exists()
    assert not (target / ".scaffold-stage").exists()
    assert not (target / ".scaffold-backup").exists()


def test_real_planning_repo_source_is_read_only_and_temp_runtime_only(tmp_path):
    for profile in SUPPORTED_PROFILES:
        assert (PLANNING_REPO / "templates" / profile).is_dir()
    runtime = tmp_path / "runtime"
    (runtime / "apps").mkdir(parents=True)
    before = {p.relative_to(PLANNING_REPO).as_posix() for p in (PLANNING_REPO / "templates").rglob("*")}
    scaffold(ScaffoldRequest("static-vite", "real-source-test", PLANNING_REPO, runtime))
    after = {p.relative_to(PLANNING_REPO).as_posix() for p in (PLANNING_REPO / "templates").rglob("*")}
    assert before == after
    assert (runtime / "apps" / "real-source-test" / "app.yaml").is_file()


@pytest.mark.parametrize(
    "profile,db_type",
    [
        ("static-vite", "none"),
        ("react-hono-sqlite", "sqlite"),
        ("react-hono-postgres", "postgres"),
        ("next-postgres", "postgres"),
        ("python-fastapi", "sqlite"),
        ("python-service", "sqlite"),
    ],
)
def test_app_yaml_profile_baseline(profile, db_type):
    content = _generate_app_yaml("my-app", profile)
    assert "schema: 1" in content
    assert "slug: my-app" in content
    assert f"profile: {profile}" in content
    assert f"type: {db_type}" in content
    assert "mode: path" in content
    assert "forward_auth: true" in content
    assert "X-authentik-username" in content


def test_existing_nonempty_target_rejected_without_explicit_overwrite(planning_repo, runtime_repo):
    target = runtime_repo / "apps" / "existing-app"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="already exists"):
        scaffold(ScaffoldRequest("static-vite", "existing-app", planning_repo, runtime_repo))
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_explicit_overwrite_replaces_stale_files_deterministically(planning_repo, runtime_repo):
    target = runtime_repo / "apps" / "existing-app"
    target.mkdir()
    (target / "stale.txt").write_text("stale", encoding="utf-8")
    result = scaffold(ScaffoldRequest("static-vite", "existing-app", planning_repo, runtime_repo, True))
    assert result["status"] == "SUCCESS"
    assert not (target / "stale.txt").exists()
    assert (target / "README.md").is_file()


def test_empty_existing_target_is_allowed(planning_repo, runtime_repo):
    (runtime_repo / "apps" / "empty-app").mkdir()
    assert scaffold(ScaffoldRequest("static-vite", "empty-app", planning_repo, runtime_repo))["status"] == "SUCCESS"


def test_nested_file_symlink_escape_rejected_before_target_mutation(planning_repo, runtime_repo, tmp_path):
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("DO-NOT-COPY", encoding="utf-8")
    source = planning_repo / "templates" / "static-vite"
    (source / "source" / "escape.txt").symlink_to(outside)
    target = runtime_repo / "apps" / "safe-app"
    with pytest.raises(ValueError, match="symlink file"):
        scaffold(ScaffoldRequest("static-vite", "safe-app", planning_repo, runtime_repo))
    assert not target.exists()


def test_nested_directory_symlink_escape_rejected_before_target_mutation(planning_repo, runtime_repo, tmp_path):
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "payload.txt").write_text("DO-NOT-COPY", encoding="utf-8")
    source = planning_repo / "templates" / "static-vite"
    (source / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink directory"):
        scaffold(ScaffoldRequest("static-vite", "safe-app", planning_repo, runtime_repo))
    assert not (runtime_repo / "apps" / "safe-app").exists()


def test_copy_failure_cleans_new_target(planning_repo, runtime_repo, monkeypatch):
    import scaffold_template as module

    original = module._copy_template

    def fail_after_write(source, target):
        original(source, target)
        raise OSError("simulated copy failure")

    monkeypatch.setattr(module, "_copy_template", fail_after_write)
    with pytest.raises(OSError, match="simulated"):
        module.scaffold(ScaffoldRequest("static-vite", "new-app", planning_repo, runtime_repo))
    assert not (runtime_repo / "apps" / "new-app").exists()


def test_copy_failure_preserves_existing_target_on_overwrite(planning_repo, runtime_repo, monkeypatch):
    import scaffold_template as module

    target = runtime_repo / "apps" / "existing-app"
    target.mkdir()
    (target / "keep.txt").write_text("original", encoding="utf-8")
    original = module._copy_template

    def fail_after_write(source, stage):
        original(source, stage)
        raise OSError("simulated copy failure")

    monkeypatch.setattr(module, "_copy_template", fail_after_write)
    with pytest.raises(OSError, match="simulated"):
        module.scaffold(ScaffoldRequest("static-vite", "existing-app", planning_repo, runtime_repo, True))
    assert (target / "keep.txt").read_text(encoding="utf-8") == "original"
    assert set(p.name for p in target.iterdir()) == {"keep.txt"}


def test_reserved_internal_target_path_rejected(planning_repo, runtime_repo):
    target = runtime_repo / "apps" / "existing-app"
    target.mkdir()
    (target / ".scaffold-stage").mkdir()
    with pytest.raises(ValueError, match="already exists|reserved"):
        scaffold(ScaffoldRequest("static-vite", "existing-app", planning_repo, runtime_repo, True))


def test_fifo_in_template_is_rejected_fail_closed(planning_repo, runtime_repo):
    if not hasattr(os, "mkfifo"):
        pytest.skip("mkfifo unavailable")
    fifo = planning_repo / "templates" / "static-vite" / "unsafe-fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="non-regular"):
        scaffold(ScaffoldRequest("static-vite", "safe-app", planning_repo, runtime_repo))
    assert not (runtime_repo / "apps" / "safe-app").exists()


def test_invalid_profile_and_slug_raise_before_writes(planning_repo, runtime_repo):
    with pytest.raises(ValueError):
        scaffold(ScaffoldRequest("invalid", "safe-app", planning_repo, runtime_repo))
    with pytest.raises(ValueError):
        scaffold(ScaffoldRequest("static-vite", "../escape", planning_repo, runtime_repo))
    assert list((runtime_repo / "apps").iterdir()) == []


def test_cli_list_profiles():
    result = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "scaffold_template.py"), "list-profiles"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert tuple(json.loads(result.stdout)["profiles"]) == SUPPORTED_PROFILES


def test_cli_validate_slug_success_and_failure():
    good = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "scaffold_template.py"), "validate-slug", "my-app"],
        capture_output=True,
        text=True,
    )
    bad = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "scaffold_template.py"), "validate-slug", "../bad"],
        capture_output=True,
        text=True,
    )
    assert good.returncode == 0 and json.loads(good.stdout)["valid"] is True
    assert bad.returncode == 1 and json.loads(bad.stdout)["valid"] is False


def test_cli_scaffold_success_and_failure(planning_repo, runtime_repo):
    base = [
        sys.executable,
        str(SCRIPT_DIR / "scaffold_template.py"),
        "scaffold",
        "--planning-repo",
        str(planning_repo),
        "--runtime-repo",
        str(runtime_repo),
    ]
    good = subprocess.run(base + ["--profile", "static-vite", "--slug", "cli-app"], capture_output=True, text=True)
    bad = subprocess.run(base + ["--profile", "bad", "--slug", "other-app"], capture_output=True, text=True)
    assert good.returncode == 0 and json.loads(good.stdout)["status"] == "SUCCESS"
    assert bad.returncode == 1 and json.loads(bad.stdout)["status"] == "FAILED"


def _load_result_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((TEMPLATES_DIR / "executor-result.schema.json").read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


def _result(status="SUCCESS", **overrides):
    data = {
        "status": status,
        "summary": "done",
        "files_changed": ["source/main.py"],
        "tests": [{"command": "pytest", "exit_code": 0, "result": "passed"}],
        "warnings": [],
        "architecture_blockers": [],
        "recommended_follow_up": [],
    }
    data.update(overrides)
    return data


def test_executor_result_success_valid():
    assert not list(_load_result_schema().iter_errors(_result()))


def test_executor_result_success_rejects_failed_test_result():
    errors = list(
        _load_result_schema().iter_errors(
            _result(tests=[{"command": "pytest", "exit_code": 0, "result": "failed"}])
        )
    )
    assert errors


def test_executor_result_success_rejects_nonzero_test_exit():
    errors = list(
        _load_result_schema().iter_errors(
            _result(tests=[{"command": "pytest", "exit_code": 1, "result": "failed"}])
        )
    )
    assert errors


def test_executor_result_success_requires_test_evidence():
    assert list(_load_result_schema().iter_errors(_result(tests=[])))


def test_executor_result_architecture_blocker_valid_without_tests_or_changes():
    blocker = _result(
        "ARCHITECTURE_BLOCKER",
        files_changed=[],
        tests=[],
        architecture_blockers=["Need an architecture decision"],
    )
    assert not list(_load_result_schema().iter_errors(blocker))


def test_executor_result_architecture_blocker_rejects_changed_files():
    blocker = _result("ARCHITECTURE_BLOCKER", tests=[], architecture_blockers=["decision"])
    assert list(_load_result_schema().iter_errors(blocker))


def test_executor_result_failed_may_have_no_tests():
    failed = _result("FAILED", tests=[], files_changed=[])
    assert not list(_load_result_schema().iter_errors(failed))


def test_executor_result_requires_all_structured_fields():
    data = _result()
    del data["warnings"]
    assert list(_load_result_schema().iter_errors(data))


@pytest.mark.parametrize("bad_path", ["../escape", "source/../../escape", "/absolute/path", r"source\windows.txt"])
def test_executor_result_rejects_unbounded_changed_paths(bad_path):
    assert list(_load_result_schema().iter_errors(_result(files_changed=[bad_path])))


def test_executor_result_schema_has_no_agent_provider_or_model_requirement():
    text = (TEMPLATES_DIR / "executor-result.schema.json").read_text(encoding="utf-8").lower()
    for forbidden in ["qwen", "gemini", "agy", "hermes", "kanban", "codex", "opencode", '"provider"', '"model"']:
        assert forbidden not in text


def test_executor_brief_is_agent_neutral_and_bounded():
    text = (TEMPLATES_DIR / "executor-brief.md").read_text(encoding="utf-8").lower()
    for forbidden in ["qwen", "gemini", "agy", "hermes", "kanban", "codex", "opencode"]:
        assert forbidden not in text
    assert "apps/<slug>/" in text
    assert "do not access the planning repo" in text
    assert "git commit" in text
    assert "deployment" in text
    assert "control-plane" in text
    assert "executor-result.schema.json" in text
    assert "self-report" in text


def test_scaffold_script_is_stdlib_only_and_agent_neutral():
    source = (SCRIPT_DIR / "scaffold_template.py").read_text(encoding="utf-8")
    lower = source.lower()
    for forbidden in ["hermes", "kanban", "agy", "gemini", "qwen", "codex", "opencode"]:
        assert forbidden not in lower
    allowed = {
        "__future__",
        "argparse",
        "json",
        "os",
        "re",
        "shutil",
        "stat",
        "sys",
        "dataclasses",
        "pathlib",
        "typing",
    }
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("import "):
            module = stripped.split()[1].split(".")[0]
            assert module in allowed
        elif stripped.startswith("from "):
            module = stripped.split()[1].split(".")[0]
            assert module in allowed


def test_template_reserved_internal_path_rejected(planning_repo, runtime_repo):
    source = planning_repo / "templates" / "static-vite"
    (source / ".scaffold-backup").mkdir()
    with pytest.raises(ValueError, match="reserved scaffold path"):
        scaffold(ScaffoldRequest("static-vite", "safe-app", planning_repo, runtime_repo))
    assert not (runtime_repo / "apps" / "safe-app").exists()
