#!/usr/bin/env python3
"""Tests for git_gate.py — Portable Core Change-Set Validation & Secret Detection.

All tests use temporary repositories and fake secrets only.
No real credentials are used.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from git_gate import (
    GitGateResult,
    SecretIncident,
    check_sensitive_file_patterns,
    detect_secrets_in_file,
    detect_secrets_in_working_tree,
    get_changed_files,
    run_git_gate,
    validate_bounded_change_set,
)


class TestBoundedChangeSet(unittest.TestCase):
    """Test bounded change set validation."""

    def test_valid_app_files(self):
        """Valid files within apps/<slug>/ should pass."""
        files = [
            "apps/my-app/src/index.ts",
            "apps/my-app/package.json",
            "apps/my-app/README.md",
        ]
        violations, escapes = validate_bounded_change_set(files, expected_slug="my-app")
        self.assertEqual(violations, [])
        self.assertEqual(escapes, [])

    def test_valid_workflow_file(self):
        """Valid deploy-<slug>.yml workflow should pass."""
        files = [".github/workflows/deploy-my-app.yml"]
        violations, escapes = validate_bounded_change_set(files, expected_slug="my-app")
        self.assertEqual(violations, [])
        self.assertEqual(escapes, [])

    def test_cross_app_change_detected(self):
        """Changes to other apps should be detected."""
        files = [
            "apps/my-app/src/index.ts",
            "apps/other-app/src/index.ts",
        ]
        violations, escapes = validate_bounded_change_set(files, expected_slug="my-app")
        self.assertEqual(len(violations), 1)
        self.assertIn("cross-app change", violations[0])
        self.assertIn("other-app", violations[0])

    def test_unexpected_workflow_file(self):
        """Unexpected workflow files should be detected."""
        files = [
            "apps/my-app/src/index.ts",
            ".github/workflows/deploy-other-app.yml",
        ]
        violations, escapes = validate_bounded_change_set(files, expected_slug="my-app")
        self.assertEqual(len(violations), 1)
        self.assertIn("unexpected workflow", violations[0])

    def test_repo_wide_change_detected(self):
        """Repo-wide changes should be detected."""
        files = [
            "apps/my-app/src/index.ts",
            "README.md",
            "package.json",
        ]
        violations, escapes = validate_bounded_change_set(files, expected_slug="my-app")
        self.assertEqual(len(violations), 2)
        self.assertTrue(any("README.md" in v for v in violations))
        self.assertTrue(any("package.json" in v for v in violations))

    def test_path_traversal_detected(self):
        """Path traversal attempts should be detected."""
        files = [
            "apps/my-app/src/../../../etc/passwd",
        ]
        violations, escapes = validate_bounded_change_set(files, expected_slug="my-app")
        self.assertEqual(len(escapes), 1)
        self.assertIn("path traversal", escapes[0])

    def test_absolute_path_detected(self):
        """Absolute paths should be detected."""
        files = ["/etc/passwd"]
        violations, escapes = validate_bounded_change_set(files, expected_slug="my-app")
        self.assertEqual(len(escapes), 1)
        self.assertIn("absolute path", escapes[0])

    def test_windows_path_separator_normalized(self):
        """Windows path separators should be normalized."""
        files = ["apps\\my-app\\src\\index.ts"]
        violations, escapes = validate_bounded_change_set(files, expected_slug="my-app")
        self.assertEqual(violations, [])
        self.assertEqual(escapes, [])

    def test_invalid_slug_format(self):
        """Invalid slug formats should be rejected."""
        files = ["apps/Invalid_Slug/src/index.ts"]
        violations, escapes = validate_bounded_change_set(files, expected_slug="Invalid_Slug")
        self.assertEqual(len(violations), 1)

    def test_no_expected_slug_allows_any_app(self):
        """Without expected_slug, any valid app path should pass."""
        files = [
            "apps/app-one/src/index.ts",
            "apps/app-two/src/index.ts",
        ]
        violations, escapes = validate_bounded_change_set(files, expected_slug=None)
        self.assertEqual(violations, [])
        self.assertEqual(escapes, [])


class TestSecretDetection(unittest.TestCase):
    """Test secret detection with fake secrets only."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_detect_fake_api_key(self):
        """Should detect fake API key patterns."""
        test_file = Path(self.temp_dir) / "config.js"
        test_file.write_text('const API_KEY = "fake_api_key_12345678";\n')

        incidents = detect_secrets_in_file(test_file)
        self.assertGreater(len(incidents), 0)
        self.assertEqual(incidents[0].classification, "CLASS_1_LOCAL_ONLY")
        self.assertFalse(incidents[0].committed)

    def test_detect_fake_aws_key(self):
        """Should detect fake AWS access key patterns."""
        test_file = Path(self.temp_dir) / "aws.js"
        test_file.write_text('const AWS_KEY = "AKIAIOSFODNN7EXAMPLE";\n')

        incidents = detect_secrets_in_file(test_file)
        self.assertGreater(len(incidents), 0)

    def test_detect_fake_github_token(self):
        """Should detect fake GitHub token patterns."""
        test_file = Path(self.temp_dir) / "github.js"
        test_file.write_text('const TOKEN = "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx";\n')

        incidents = detect_secrets_in_file(test_file)
        self.assertGreater(len(incidents), 0)

    def test_detect_fake_private_key(self):
        """Should detect fake private key headers."""
        test_file = Path(self.temp_dir) / "key.pem"
        test_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nfake_key_data\n")

        incidents = detect_secrets_in_file(test_file)
        self.assertGreater(len(incidents), 0)

    def test_no_false_positive_on_example_values(self):
        """Should not flag obvious placeholder values."""
        test_file = Path(self.temp_dir) / "env.example"
        test_file.write_text("API_KEY=your_api_key_here\n")

        incidents = detect_secrets_in_file(test_file)
        # Short placeholder should not match (min length 8)
        self.assertEqual(len(incidents), 0)

    def test_detect_secrets_in_working_tree(self):
        """Should detect secrets in working tree files."""
        test_file = Path(self.temp_dir) / "config.js"
        test_file.write_text('const SECRET = "fake_secret_1234567890123456";\n')

        incidents = detect_secrets_in_working_tree(
            Path(self.temp_dir), ["config.js"]
        )
        self.assertGreater(len(incidents), 0)


class TestSensitiveFilePatterns(unittest.TestCase):
    """Test sensitive file pattern detection."""

    def test_env_file_detected(self):
        """Should detect .env files."""
        violations = check_sensitive_file_patterns([".env", ".env.local"])
        self.assertEqual(len(violations), 2)

    def test_pem_file_detected(self):
        """Should detect .pem files."""
        violations = check_sensitive_file_patterns(["cert.pem"])
        self.assertEqual(len(violations), 1)

    def test_key_file_detected(self):
        """Should detect .key files."""
        violations = check_sensitive_file_patterns(["private.key"])
        self.assertEqual(len(violations), 1)

    def test_normal_files_not_detected(self):
        """Should not flag normal files."""
        violations = check_sensitive_file_patterns(["index.ts", "package.json"])
        self.assertEqual(len(violations), 0)


class TestGitGateIntegration(unittest.TestCase):
    """Integration tests with temporary git repositories."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.repo_root = Path(self.temp_dir) / "repo"
        self.repo_root.mkdir()

        # Initialize git repo
        subprocess.run(
            ["git", "init"],
            cwd=self.repo_root,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=self.repo_root,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=self.repo_root,
            capture_output=True,
            check=True,
        )

        # Create initial commit
        readme = self.repo_root / "README.md"
        readme.write_text("# Test Repo\n")
        subprocess.run(
            ["git", "add", "README.md"],
            cwd=self.repo_root,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=self.repo_root,
            capture_output=True,
            check=True,
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_clean_working_tree_passes(self):
        """Clean working tree should pass git gate."""
        result = run_git_gate(self.repo_root, expected_slug="my-app")
        self.assertTrue(result.valid)
        self.assertEqual(result.violations, [])
        self.assertEqual(result.secret_incidents, [])

    def test_valid_app_change_passes(self):
        """Valid app changes should pass git gate."""
        app_dir = self.repo_root / "apps" / "my-app"
        app_dir.mkdir(parents=True)
        (app_dir / "index.ts").write_text("console.log('hello');\n")

        result = run_git_gate(self.repo_root, expected_slug="my-app")
        self.assertTrue(result.valid)
        self.assertIn("apps/my-app/index.ts", result.changed_files)

    def test_cross_app_change_fails(self):
        """Cross-app changes should fail git gate."""
        app1_dir = self.repo_root / "apps" / "my-app"
        app1_dir.mkdir(parents=True)
        (app1_dir / "index.ts").write_text("console.log('hello');\n")

        app2_dir = self.repo_root / "apps" / "other-app"
        app2_dir.mkdir(parents=True)
        (app2_dir / "index.ts").write_text("console.log('other');\n")

        result = run_git_gate(self.repo_root, expected_slug="my-app")
        self.assertFalse(result.valid)
        self.assertTrue(any("cross-app" in v for v in result.violations))

    def test_secret_in_working_tree_fails(self):
        """Secrets in working tree should fail git gate."""
        app_dir = self.repo_root / "apps" / "my-app"
        app_dir.mkdir(parents=True)
        (app_dir / "config.js").write_text('const API_KEY = "fake_key_1234567890123456";\n')

        result = run_git_gate(self.repo_root, expected_slug="my-app")
        self.assertFalse(result.valid)
        self.assertGreater(len(result.secret_incidents), 0)
        self.assertEqual(result.secret_incidents[0]["classification"], "CLASS_1_LOCAL_ONLY")

    def test_env_file_detected(self):
        """Env files should be detected as sensitive."""
        app_dir = self.repo_root / "apps" / "my-app"
        app_dir.mkdir(parents=True)
        (app_dir / ".env").write_text("SECRET=value\n")

        result = run_git_gate(self.repo_root, expected_slug="my-app")
        self.assertFalse(result.valid)
        self.assertTrue(any(".env" in v for v in result.violations))

    def test_path_traversal_fails(self):
        """Path traversal attempts should fail git gate."""
        # Create a file outside apps/
        (self.repo_root / "evil.txt").write_text("malicious\n")

        result = run_git_gate(self.repo_root, expected_slug="my-app")
        self.assertFalse(result.valid)
        self.assertTrue(any("evil.txt" in v for v in result.violations))

    def test_unexpected_workflow_fails(self):
        """Unexpected workflow files should fail git gate."""
        workflow_dir = self.repo_root / ".github" / "workflows"
        workflow_dir.mkdir(parents=True)
        (workflow_dir / "deploy-other-app.yml").write_text("name: deploy\n")

        result = run_git_gate(self.repo_root, expected_slug="my-app")
        self.assertFalse(result.valid)
        self.assertTrue(any("unexpected workflow" in v for v in result.violations))

    def test_valid_workflow_passes(self):
        """Valid deploy-<slug>.yml should pass git gate."""
        workflow_dir = self.repo_root / ".github" / "workflows"
        workflow_dir.mkdir(parents=True)
        (workflow_dir / "deploy-my-app.yml").write_text("name: deploy\n")

        result = run_git_gate(self.repo_root, expected_slug="my-app")
        self.assertTrue(result.valid)


class TestSecretIncidentContract(unittest.TestCase):
    """Test Two-Class Secret Incident Contract."""

    def test_class_1_local_only(self):
        """Class 1: Secret only in working tree."""
        incident = SecretIncident(
            classification="CLASS_1_LOCAL_ONLY",
            file_path="apps/my-app/config.js",
            line_number=5,
            pattern_matched="fake_pattern",
            committed=False,
        )
        self.assertEqual(incident.classification, "CLASS_1_LOCAL_ONLY")
        self.assertFalse(incident.committed)

    def test_class_2_exposed(self):
        """Class 2: Secret already committed/exposed."""
        incident = SecretIncident(
            classification="CLASS_2_EXPOSED",
            file_path="apps/my-app/config.js",
            line_number=5,
            pattern_matched="fake_pattern",
            committed=True,
        )
        self.assertEqual(incident.classification, "CLASS_2_EXPOSED")
        self.assertTrue(incident.committed)

    def test_incident_to_dict(self):
        """Incident should serialize to dict."""
        incident = SecretIncident(
            classification="CLASS_1_LOCAL_ONLY",
            file_path="test.js",
            line_number=10,
            pattern_matched="pattern",
            committed=False,
        )
        d = incident.to_dict()
        self.assertEqual(d["classification"], "CLASS_1_LOCAL_ONLY")
        self.assertEqual(d["file_path"], "test.js")
        self.assertEqual(d["line_number"], 10)
        self.assertFalse(d["committed"])


class TestGitGateResult(unittest.TestCase):
    """Test GitGateResult serialization."""

    def test_result_to_dict(self):
        """Result should serialize to dict."""
        result = GitGateResult(
            valid=True,
            changed_files=["apps/my-app/index.ts"],
            violations=[],
            secret_incidents=[],
            scope_escapes=[],
        )
        d = result.to_dict()
        self.assertTrue(d["valid"])
        self.assertEqual(d["changed_files"], ["apps/my-app/index.ts"])
        self.assertEqual(d["violations"], [])

    def test_invalid_result_to_dict(self):
        """Invalid result should serialize correctly."""
        result = GitGateResult(
            valid=False,
            changed_files=["apps/my-app/index.ts"],
            violations=["scope violation"],
            secret_incidents=[],
            scope_escapes=[],
        )
        d = result.to_dict()
        self.assertFalse(d["valid"])
        self.assertEqual(d["violations"], ["scope violation"])


if __name__ == "__main__":
    unittest.main()
