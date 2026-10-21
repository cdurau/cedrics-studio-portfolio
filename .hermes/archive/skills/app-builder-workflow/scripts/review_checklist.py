#!/usr/bin/env python3
"""
review_checklist.py — Agent-neutral Review Checklist for Portable App Builder Core.

This module defines the canonical review criteria that any reviewer (human or AI)
must evaluate. It produces a structured review result with:
- Scope validation
- Diff verification
- Test evidence (command + exit code)
- Findings (HIGH / MEDIUM / LOW)
- External effects verification
- Senior review requirement determination
- Decision: ready / changes_required / escalate

Max 2 correction rounds as Core policy. After that: ESCALATE or STOP.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class Severity(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ReviewVerdict(str, Enum):
    READY = "ready"
    CHANGES_REQUIRED = "changes_required"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class ReviewFinding:
    """A single review finding."""
    severity: Severity
    description: str
    file: Optional[str] = None
    line: Optional[int] = None

    def to_dict(self) -> dict:
        result = {
            "severity": self.severity.value,
            "description": self.description,
        }
        if self.file:
            result["file"] = self.file
        if self.line is not None:
            result["line"] = self.line
        return result


@dataclass(frozen=True)
class TestEvidence:
    """Test execution evidence."""
    command: str
    exit_code: int
    result: str  # "passed" or "failed"
    output_summary: Optional[str] = None

    def to_dict(self) -> dict:
        result = {
            "command": self.command,
            "exit_code": self.exit_code,
            "result": self.result,
        }
        if self.output_summary:
            result["output_summary"] = self.output_summary
        return result


@dataclass(frozen=True)
class ReviewChecklistResult:
    """Complete review checklist result."""
    verdict: ReviewVerdict
    summary: str
    scope_valid: bool
    diff_verified: bool
    tests: List[TestEvidence]
    findings: List[ReviewFinding]
    external_effects_verified: bool
    senior_review_required: bool
    senior_review_reason: Optional[str]
    correction_round: int
    max_corrections: int = 2
    recommendation: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "summary": self.summary,
            "scope_valid": self.scope_valid,
            "diff_verified": self.diff_verified,
            "tests": [t.to_dict() for t in self.tests],
            "findings": [f.to_dict() for f in self.findings],
            "external_effects_verified": self.external_effects_verified,
            "senior_review_required": self.senior_review_required,
            "senior_review_reason": self.senior_review_reason,
            "correction_round": self.correction_round,
            "max_corrections": self.max_corrections,
            "recommendation": self.recommendation,
        }


# Mandatory Senior Review Triggers (Core Policy)
SENIOR_REVIEW_TRIGGERS = [
    "complex_new_architecture",
    "coupled_control_planes",
    "security_authority_boundary_changes",
    "git_deployment_infra_authority_changes",
    "complex_final_acceptance",
    "contradictory_executor_results",
    "orchestrator_uncertainty",
]


def evaluate_scope(scope_valid: bool) -> List[ReviewFinding]:
    """Evaluate scope compliance."""
    if not scope_valid:
        return [
            ReviewFinding(
                severity=Severity.HIGH,
                description="Scope violation detected — changes outside bounded change set",
            )
        ]
    return []


def evaluate_diff(diff_verified: bool) -> List[ReviewFinding]:
    """Evaluate diff verification."""
    if not diff_verified:
        return [
            ReviewFinding(
                severity=Severity.HIGH,
                description="Diff not verified — actual changes not inspected",
            )
        ]
    return []


def evaluate_tests(tests: List[TestEvidence]) -> List[ReviewFinding]:
    """Evaluate test evidence."""
    findings = []

    if not tests:
        findings.append(
            ReviewFinding(
                severity=Severity.HIGH,
                description="No test evidence provided — fail-closed requires test execution",
            )
        )
        return findings

    failed_tests = [t for t in tests if t.result != "passed"]
    if failed_tests:
        for test in failed_tests:
            findings.append(
                ReviewFinding(
                    severity=Severity.HIGH,
                    description=f"Test failed: {test.command} (exit code {test.exit_code})",
                )
            )

    return findings


def evaluate_external_effects(verified: bool) -> List[ReviewFinding]:
    """Evaluate external effects verification."""
    if not verified:
        return [
            ReviewFinding(
                severity=Severity.MEDIUM,
                description="External effects not verified or marked N/A without justification",
            )
        ]
    return []


def determine_senior_review_required(
    architecture_complex: bool = False,
    coupled_control_planes: bool = False,
    security_boundary_changes: bool = False,
    git_deploy_infra_authority_changes: bool = False,
    complex_final_acceptance: bool = False,
    contradictory_results: bool = False,
    orchestrator_uncertainty: bool = False,
) -> Tuple[bool, Optional[str]]:
    """Determine if senior review is required based on Core policy triggers."""
    reasons = []

    if architecture_complex:
        reasons.append("complex_new_architecture")
    if coupled_control_planes:
        reasons.append("coupled_control_planes")
    if security_boundary_changes:
        reasons.append("security_authority_boundary_changes")
    if git_deploy_infra_authority_changes:
        reasons.append("git_deployment_infra_authority_changes")
    if complex_final_acceptance:
        reasons.append("complex_final_acceptance")
    if contradictory_results:
        reasons.append("contradictory_executor_results")
    if orchestrator_uncertainty:
        reasons.append("orchestrator_uncertainty")

    if reasons:
        return True, ", ".join(reasons)
    return False, None


def determine_verdict(
    scope_valid: bool,
    diff_verified: bool,
    tests: List[TestEvidence],
    findings: List[ReviewFinding],
    external_effects_verified: bool,
    correction_round: int,
    max_corrections: int = 2,
) -> Tuple[ReviewVerdict, str, Optional[str]]:
    """Determine the review verdict based on all criteria."""
    
    # Check for critical failures
    high_findings = [f for f in findings if f.severity == Severity.HIGH]
    
    if not scope_valid:
        return ReviewVerdict.ESCALATE, "Scope violation — escalate immediately", "Scope violation requires immediate escalation"
    
    if not diff_verified:
        return ReviewVerdict.ESCALATE, "Diff not verified — cannot approve", "Diff verification is mandatory"
    
    if not tests or any(t.result != "passed" for t in tests):
        return ReviewVerdict.CHANGES_REQUIRED, "Tests missing or failed", "Fix failing tests before proceeding"
    
    if high_findings:
        if correction_round >= max_corrections:
            return ReviewVerdict.ESCALATE, f"Max correction rounds ({max_corrections}) exceeded with HIGH findings", "Escalate after max corrections"
        return ReviewVerdict.CHANGES_REQUIRED, f"HIGH findings detected ({len(high_findings)})", "Address HIGH findings before proceeding"
    
    # Check for medium findings
    medium_findings = [f for f in findings if f.severity == Severity.MEDIUM]
    if medium_findings:
        if correction_round >= max_corrections:
            return ReviewVerdict.ESCALATE, f"Max correction rounds ({max_corrections}) exceeded with MEDIUM findings", "Escalate after max corrections"
        return ReviewVerdict.CHANGES_REQUIRED, f"MEDIUM findings detected ({len(medium_findings)})", "Address MEDIUM findings"
    
    # All checks passed
    return ReviewVerdict.READY, "All review criteria satisfied", "Ready for deployment"


def run_review_checklist(
    scope_valid: bool,
    diff_verified: bool,
    tests: List[TestEvidence],
    external_effects_verified: bool,
    correction_round: int = 0,
    architecture_complex: bool = False,
    coupled_control_planes: bool = False,
    security_boundary_changes: bool = False,
    git_deploy_infra_authority_changes: bool = False,
    complex_final_acceptance: bool = False,
    contradictory_results: bool = False,
    orchestrator_uncertainty: bool = False,
    additional_findings: Optional[List[ReviewFinding]] = None,
) -> ReviewChecklistResult:
    """Run the complete review checklist.
    
    Args:
        scope_valid: Whether scope boundaries are respected
        diff_verified: Whether actual diff was inspected
        tests: List of test evidence
        external_effects_verified: Whether external effects were verified or N/A
        correction_round: Current correction round (0-based)
        architecture_complex: Senior review trigger
        coupled_control_planes: Senior review trigger
        security_boundary_changes: Senior review trigger
        git_deploy_infra_authority_changes: Senior review trigger
        complex_final_acceptance: Senior review trigger
        contradictory_results: Senior review trigger
        orchestrator_uncertainty: Senior review trigger
        additional_findings: Extra findings from reviewer
    
    Returns:
        ReviewChecklistResult with verdict and all details
    """
    all_findings = []

    # Evaluate core criteria
    all_findings.extend(evaluate_scope(scope_valid))
    all_findings.extend(evaluate_diff(diff_verified))
    all_findings.extend(evaluate_tests(tests))
    all_findings.extend(evaluate_external_effects(external_effects_verified))

    # Add additional findings
    if additional_findings:
        all_findings.extend(additional_findings)

    # Determine senior review requirement
    senior_required, senior_reason = determine_senior_review_required(
        architecture_complex=architecture_complex,
        coupled_control_planes=coupled_control_planes,
        security_boundary_changes=security_boundary_changes,
        git_deploy_infra_authority_changes=git_deploy_infra_authority_changes,
        complex_final_acceptance=complex_final_acceptance,
        contradictory_results=contradictory_results,
        orchestrator_uncertainty=orchestrator_uncertainty,
    )

    # Determine verdict
    verdict, summary, recommendation = determine_verdict(
        scope_valid=scope_valid,
        diff_verified=diff_verified,
        tests=tests,
        findings=all_findings,
        external_effects_verified=external_effects_verified,
        correction_round=correction_round,
    )

    return ReviewChecklistResult(
        verdict=verdict,
        summary=summary,
        scope_valid=scope_valid,
        diff_verified=diff_verified,
        tests=tests,
        findings=all_findings,
        external_effects_verified=external_effects_verified,
        senior_review_required=senior_required,
        senior_review_reason=senior_reason,
        correction_round=correction_round,
        recommendation=recommendation,
    )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Review Checklist — Agent-neutral review criteria (Portable Core)"
    )
    parser.add_argument("--scope-valid", action="store_true", help="Scope boundaries respected")
    parser.add_argument("--diff-verified", action="store_true", help="Actual diff inspected")
    parser.add_argument("--external-effects-verified", action="store_true", help="External effects verified")
    parser.add_argument("--correction-round", type=int, default=0, help="Current correction round")
    parser.add_argument("--architecture-complex", action="store_true", help="Senior review trigger")
    parser.add_argument("--coupled-control-planes", action="store_true", help="Senior review trigger")
    parser.add_argument("--security-boundary-changes", action="store_true", help="Senior review trigger")
    parser.add_argument("--git-deploy-infra-authority-changes", action="store_true", help="Senior review trigger")
    parser.add_argument("--complex-final-acceptance", action="store_true", help="Senior review trigger")
    parser.add_argument("--contradictory-results", action="store_true", help="Senior review trigger")
    parser.add_argument("--orchestrator-uncertainty", action="store_true", help="Senior review trigger")
    parser.add_argument("--tests-json", help="JSON string with test evidence array")
    parser.add_argument("--findings-json", help="JSON string with additional findings array")

    args = parser.parse_args()

    # Parse tests
    tests = []
    if args.tests_json:
        tests_data = json.loads(args.tests_json)
        for t in tests_data:
            tests.append(
                TestEvidence(
                    command=t["command"],
                    exit_code=t["exit_code"],
                    result=t["result"],
                    output_summary=t.get("output_summary"),
                )
            )

    # Parse findings
    additional_findings = []
    if args.findings_json:
        findings_data = json.loads(args.findings_json)
        for f in findings_data:
            additional_findings.append(
                ReviewFinding(
                    severity=Severity(f["severity"]),
                    description=f["description"],
                    file=f.get("file"),
                    line=f.get("line"),
                )
            )

    result = run_review_checklist(
        scope_valid=args.scope_valid,
        diff_verified=args.diff_verified,
        tests=tests,
        external_effects_verified=args.external_effects_verified,
        correction_round=args.correction_round,
        architecture_complex=args.architecture_complex,
        coupled_control_planes=args.coupled_control_planes,
        security_boundary_changes=args.security_boundary_changes,
        git_deploy_infra_authority_changes=args.git_deploy_infra_authority_changes,
        complex_final_acceptance=args.complex_final_acceptance,
        contradictory_results=args.contradictory_results,
        orchestrator_uncertainty=args.orchestrator_uncertainty,
        additional_findings=additional_findings,
    )

    print(json.dumps(result.to_dict(), indent=2))


if __name__ == "__main__":
    from typing import Tuple
    main()
