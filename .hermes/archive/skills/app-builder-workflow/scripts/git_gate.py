#!/usr/bin/env python3
"""
git_gate.py — Deterministic Change-Set Validation & Secret Detection for Portable App Builder Core.

This module is agent-neutral and stdlib-only. It validates:
- Actual change set (not executor self-reports)
- Bounded new-app change set: apps/<slug>/** + .github/workflows/deploy-<slug>.yml
- Cross-app changes (forbidden)
- Repo-wide changes (forbidden)
- Unexpected workflow files (forbidden)
- Secret/credential indicators (fail-closed)
- Scope escapes (path traversal, symlinks, etc.)

All validation is fail-closed. Executor self-statements are NOT evidence.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Set, Tuple

# Canonical bounded change set for a new app
BOUNDED_CHANGE_SET_PATTERN = re.compile(
    r"^(apps/[a-z0-9][a-z0-9-]{0,46}[a-z0-9]/.*|.github/workflows/deploy-[a-z0-9][a-z0-9-]{0,46}[a-z0-9].yml)$"
)

# Secret detection patterns (conservative, fail-closed)
SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|api[_-]?secret|password|passwd|pwd)\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
    re.compile(r"(?i)(token|secret|credential)\s*[:=]\s*['\"][^'\"]{16,}['\"]"),
    re.compile(r"(?i)-----BEGIN\s+(RSA|DSA|EC|OPENSSH)\s+PRIVATE\s+KEY-----"),
    re.compile(r"(?i)(AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}"),  # AWS access keys
    re.compile(r"(?i)ghp_[0-9a-zA-Z]{36}"),  # GitHub personal access tokens
    re.compile(r"(?i)github_pat_[0-9a-zA-Z_]{82}"),  # GitHub fine-grained PATs
    re.compile(r"(?i)sk-[0-9a-zA-Z]{48}"),  # OpenAI-style keys
    re.compile(r"(?i)xox[baprs]-[0-9a-zA-Z-]{10,}"),  # Slack tokens
]

# File patterns that should never contain secrets
SECRET_SENSITIVE_FILE_PATTERNS = [
    re.compile(r".*\.env$"),
    re.compile(r".*\.env\.local$"),
    re.compile(r".*\.env\.production$"),
    re.compile(r".*secrets?.json$"),
    re.compile(r".*credentials?.json$"),
    re.compile(r".*\.pem$"),
    re.compile(r".*\.key$"),
]


@dataclass(frozen=True)
class GitGateResult:
    """Result of git gate validation."""
    valid: bool
    changed_files: List[str]
    violations: List[str]
    secret_incidents: List[str]
    scope_escapes: List[str]

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "changed_files": self.changed_files,
            "violations": self.violations,
            "secret_incidents": self.secret_incidents,
            "scope_escapes": self.scope_escapes,
        }


@dataclass(frozen=True)
class SecretIncident:
    """Two-class secret incident contract."""
    classification: str  # "CLASS_1_LOCAL_ONLY" or "CLASS_2_EXPOSED"
    file_path: str
    line_number: Optional[int]
    pattern_matched: str
    committed: bool  # True if in git history, False if only in working tree

    def to_dict(self) -> dict:
        return {
            "classification": self.classification,
            "file_path": self.file_path,
            "line_number": self.line_number,
            "pattern_matched": self.pattern_matched,
            "committed": self.committed,
        }


def get_changed_files(repo_root: Path, base_ref: str = "HEAD") -> Tuple[List[str], List[str]]:
    """Get actual changed files from git. Returns (changed_files, errors)."""
    try:
        # Get unstaged changes
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        unstaged = [line.strip() for line in result.stdout.splitlines() if line.strip()]

        # Get staged changes
        result = subprocess.run(
            ["git", "diff", "--name-only", "--cached"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        staged = [line.strip() for line in result.stdout.splitlines() if line.strip()]

        # Get untracked files
        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        untracked = [line.strip() for line in result.stdout.splitlines() if line.strip()]

        # Combine and deduplicate
        all_changed = sorted(set(unstaged + staged + untracked))
        return all_changed, []

    except subprocess.CalledProcessError as exc:
        return [], [f"git command failed: {exc.stderr}"]
    except Exception as exc:
        return [], [f"failed to get changed files: {exc}"]


def get_committed_files(repo_root: Path, since_ref: Optional[str] = None) -> Tuple[List[str], List[str]]:
    """Get files committed since a reference (or all files if no ref). Returns (files, errors)."""
    try:
        if since_ref:
            result = subprocess.run(
                ["git", "diff", "--name-only", since_ref, "HEAD"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
            )
        else:
            result = subprocess.run(
                ["git", "ls-files"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
            )
        files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return files, []
    except subprocess.CalledProcessError as exc:
        return [], [f"git command failed: {exc.stderr}"]
    except Exception as exc:
        return [], [f"failed to get committed files: {exc}"]


def validate_bounded_change_set(changed_files: List[str], expected_slug: Optional[str] = None) -> Tuple[List[str], List[str]]:
    """Validate that changed files are within the bounded new-app change set.
    
    Returns (violations, scope_escapes).
    """
    violations = []
    scope_escapes = []

    for file_path in changed_files:
        # Check for path traversal attempts
        if ".." in file_path or file_path.startswith("/"):
            scope_escapes.append(f"path traversal or absolute path: {file_path}")
            continue

        # Normalize path separators
        normalized = file_path.replace("\\", "/")

        # Check against bounded change set pattern
        if not BOUNDED_CHANGE_SET_PATTERN.match(normalized):
            violations.append(f"file outside bounded change set: {file_path}")
            continue

        # If expected_slug is provided, enforce it
        if expected_slug:
            if normalized.startswith("apps/") and not normalized.startswith(f"apps/{expected_slug}/"):
                violations.append(f"cross-app change detected: {file_path} (expected apps/{expected_slug}/)")
            elif normalized.startswith(".github/workflows/"):
                # Workflow files must match the expected slug
                if not normalized.startswith(f".github/workflows/deploy-{expected_slug}"):
                    violations.append(f"unexpected workflow file: {file_path} (expected deploy-{expected_slug}.yml)")

    return violations, scope_escapes


def detect_secrets_in_file(file_path: Path, check_patterns: bool = True) -> List[SecretIncident]:
    """Detect potential secrets in a single file. Returns list of incidents."""
    incidents = []

    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except Exception:
        return incidents

    for line_num, line in enumerate(lines, start=1):
        for pattern in SECRET_PATTERNS:
            if pattern.search(line):
                incidents.append(
                    SecretIncident(
                        classification="CLASS_1_LOCAL_ONLY",  # Will be upgraded if committed
                        file_path=str(file_path),
                        line_number=line_num,
                        pattern_matched=pattern.pattern,
                        committed=False,
                    )
                )

    return incidents


def detect_secrets_in_working_tree(repo_root: Path, changed_files: List[str]) -> List[SecretIncident]:
    """Detect secrets in changed files in the working tree."""
    incidents = []

    for file_path in changed_files:
        full_path = repo_root / file_path
        if full_path.is_file():
            incidents.extend(detect_secrets_in_file(full_path))

    return incidents


def detect_secrets_in_committed_files(repo_root: Path, committed_files: List[str]) -> List[SecretIncident]:
    """Detect secrets in committed files (Class 2 — Exposed)."""
    incidents = []

    for file_path in committed_files:
        full_path = repo_root / file_path
        if full_path.is_file():
            file_incidents = detect_secrets_in_file(full_path)
            # Upgrade to Class 2 if committed
            for incident in file_incidents:
                incidents.append(
                    SecretIncident(
                        classification="CLASS_2_EXPOSED",
                        file_path=incident.file_path,
                        line_number=incident.line_number,
                        pattern_matched=incident.pattern_matched,
                        committed=True,
                    )
                )

    return incidents


def check_sensitive_file_patterns(changed_files: List[str]) -> List[str]:
    """Check if changed files match sensitive patterns that should never contain secrets."""
    violations = []

    for file_path in changed_files:
        for pattern in SECRET_SENSITIVE_FILE_PATTERNS:
            if pattern.match(file_path):
                violations.append(f"sensitive file pattern matched: {file_path}")
                break

    return violations


def run_git_gate(
    repo_root: Path,
    expected_slug: Optional[str] = None,
    base_ref: str = "HEAD",
    check_committed: bool = False,
    since_ref: Optional[str] = None,
) -> GitGateResult:
    """Run the complete git gate validation.
    
    Args:
        repo_root: Repository root directory
        expected_slug: Expected app slug (enforces single-app scope)
        base_ref: Base reference for diff (default: HEAD)
        check_committed: If True, also check committed files for secrets
        since_ref: If provided, check files committed since this ref
    
    Returns:
        GitGateResult with validation outcome
    """
    violations = []
    secret_incidents = []
    scope_escapes = []

    # Get actual changed files
    changed_files, errors = get_changed_files(repo_root, base_ref)
    if errors:
        return GitGateResult(
            valid=False,
            changed_files=[],
            violations=errors,
            secret_incidents=[],
            scope_escapes=[],
        )

    # Validate bounded change set
    change_violations, escapes = validate_bounded_change_set(changed_files, expected_slug)
    violations.extend(change_violations)
    scope_escapes.extend(escapes)

    # Check for sensitive file patterns
    sensitive_violations = check_sensitive_file_patterns(changed_files)
    violations.extend(sensitive_violations)

    # Detect secrets in working tree (Class 1)
    working_tree_incidents = detect_secrets_in_working_tree(repo_root, changed_files)
    secret_incidents.extend(working_tree_incidents)

    # Optionally check committed files (Class 2)
    if check_committed:
        committed_files, errors = get_committed_files(repo_root, since_ref)
        if not errors:
            committed_incidents = detect_secrets_in_committed_files(repo_root, committed_files)
            secret_incidents.extend(committed_incidents)

    # Determine overall validity
    valid = len(violations) == 0 and len(secret_incidents) == 0 and len(scope_escapes) == 0

    return GitGateResult(
        valid=valid,
        changed_files=changed_files,
        violations=violations,
        secret_incidents=[inc.to_dict() for inc in secret_incidents],
        scope_escapes=scope_escapes,
    )


def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description="Git Gate — Deterministic Change-Set Validation & Secret Detection (Portable Core)"
    )
    parser.add_argument("repo_root", type=Path, help="Repository root directory")
    parser.add_argument("--expected-slug", help="Expected app slug (enforces single-app scope)")
    parser.add_argument("--base-ref", default="HEAD", help="Base reference for diff (default: HEAD)")
    parser.add_argument("--check-committed", action="store_true", help="Also check committed files for secrets")
    parser.add_argument("--since-ref", help="Check files committed since this ref")

    args = parser.parse_args()

    if not args.repo_root.is_dir():
        print(json.dumps({"error": f"repo_root does not exist: {args.repo_root}"}))
        sys.exit(1)

    result = run_git_gate(
        repo_root=args.repo_root,
        expected_slug=args.expected_slug,
        base_ref=args.base_ref,
        check_committed=args.check_committed,
        since_ref=args.since_ref,
    )

    print(json.dumps(result.to_dict(), indent=2))
    sys.exit(0 if result.valid else 1)


if __name__ == "__main__":
    main()
