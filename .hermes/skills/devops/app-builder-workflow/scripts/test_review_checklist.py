#!/usr/bin/env python3
"""Tests for review_checklist.py — Portable Core Review Criteria.

All tests use fake data and test fixtures only.
"""

import json
import unittest

from review_checklist import (
    ReviewChecklistResult,
    ReviewFinding,
    ReviewVerdict,
    Severity,
    TestEvidence,
    determine_senior_review_required,
    determine_verdict,
    evaluate_diff,
    evaluate_external_effects,
    evaluate_scope,
    evaluate_tests,
    run_review_checklist,
)


class TestSeverity(unittest.TestCase):
    """Test Severity enum."""

    def test_severity_values(self):
        """Severity should have correct values."""
        self.assertEqual(Severity.HIGH.value, "HIGH")
        self.assertEqual(Severity.MEDIUM.value, "MEDIUM")
        self.assertEqual(Severity.LOW.value, "LOW")


class TestReviewVerdict(unittest.TestCase):
    """Test ReviewVerdict enum."""

    def test_verdict_values(self):
        """Verdict should have correct values."""
        self.assertEqual(ReviewVerdict.READY.value, "ready")
        self.assertEqual(ReviewVerdict.CHANGES_REQUIRED.value, "changes_required")
        self.assertEqual(ReviewVerdict.ESCALATE.value, "escalate")


class TestReviewFinding(unittest.TestCase):
    """Test ReviewFinding dataclass."""

    def test_finding_to_dict(self):
        """Finding should serialize to dict."""
        finding = ReviewFinding(
            severity=Severity.HIGH,
            description="Test finding",
            file="test.py",
            line=10,
        )
        d = finding.to_dict()
        self.assertEqual(d["severity"], "HIGH")
        self.assertEqual(d["description"], "Test finding")
        self.assertEqual(d["file"], "test.py")
        self.assertEqual(d["line"], 10)

    def test_finding_without_optional_fields(self):
        """Finding without optional fields should serialize correctly."""
        finding = ReviewFinding(
            severity=Severity.MEDIUM,
            description="Test finding",
        )
        d = finding.to_dict()
        self.assertEqual(d["severity"], "MEDIUM")
        self.assertNotIn("file", d)
        self.assertNotIn("line", d)


class TestTestEvidence(unittest.TestCase):
    """Test TestEvidence dataclass."""

    def test_test_evidence_to_dict(self):
        """Test evidence should serialize to dict."""
        evidence = TestEvidence(
            command="pytest tests/",
            exit_code=0,
            result="passed",
            output_summary="10 tests passed",
        )
        d = evidence.to_dict()
        self.assertEqual(d["command"], "pytest tests/")
        self.assertEqual(d["exit_code"], 0)
        self.assertEqual(d["result"], "passed")
        self.assertEqual(d["output_summary"], "10 tests passed")

    def test_failed_test_evidence(self):
        """Failed test evidence should serialize correctly."""
        evidence = TestEvidence(
            command="pytest tests/",
            exit_code=1,
            result="failed",
        )
        d = evidence.to_dict()
        self.assertEqual(d["exit_code"], 1)
        self.assertEqual(d["result"], "failed")


class TestEvaluateScope(unittest.TestCase):
    """Test scope evaluation."""

    def test_valid_scope(self):
        """Valid scope should return no findings."""
        findings = evaluate_scope(scope_valid=True)
        self.assertEqual(findings, [])

    def test_invalid_scope(self):
        """Invalid scope should return HIGH finding."""
        findings = evaluate_scope(scope_valid=False)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, Severity.HIGH)
        self.assertIn("Scope violation", findings[0].description)


class TestEvaluateDiff(unittest.TestCase):
    """Test diff evaluation."""

    def test_verified_diff(self):
        """Verified diff should return no findings."""
        findings = evaluate_diff(diff_verified=True)
        self.assertEqual(findings, [])

    def test_unverified_diff(self):
        """Unverified diff should return HIGH finding."""
        findings = evaluate_diff(diff_verified=False)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, Severity.HIGH)
        self.assertIn("Diff not verified", findings[0].description)


class TestEvaluateTests(unittest.TestCase):
    """Test test evidence evaluation."""

    def test_no_tests(self):
        """No tests should return HIGH finding."""
        findings = evaluate_tests(tests=[])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, Severity.HIGH)
        self.assertIn("No test evidence", findings[0].description)

    def test_all_tests_passed(self):
        """All passed tests should return no findings."""
        tests = [
            TestEvidence(command="pytest", exit_code=0, result="passed"),
            TestEvidence(command="npm test", exit_code=0, result="passed"),
        ]
        findings = evaluate_tests(tests=tests)
        self.assertEqual(findings, [])

    def test_failed_test(self):
        """Failed test should return HIGH finding."""
        tests = [
            TestEvidence(command="pytest", exit_code=1, result="failed"),
        ]
        findings = evaluate_tests(tests=tests)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, Severity.HIGH)
        self.assertIn("Test failed", findings[0].description)

    def test_mixed_test_results(self):
        """Mixed test results should return findings for failed tests."""
        tests = [
            TestEvidence(command="pytest", exit_code=0, result="passed"),
            TestEvidence(command="npm test", exit_code=1, result="failed"),
        ]
        findings = evaluate_tests(tests=tests)
        self.assertEqual(len(findings), 1)
        self.assertIn("npm test", findings[0].description)


class TestEvaluateExternalEffects(unittest.TestCase):
    """Test external effects evaluation."""

    def test_verified_external_effects(self):
        """Verified external effects should return no findings."""
        findings = evaluate_external_effects(verified=True)
        self.assertEqual(findings, [])

    def test_unverified_external_effects(self):
        """Unverified external effects should return MEDIUM finding."""
        findings = evaluate_external_effects(verified=False)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, Severity.MEDIUM)
        self.assertIn("External effects not verified", findings[0].description)


class TestDetermineSeniorReviewRequired(unittest.TestCase):
    """Test senior review requirement determination."""

    def test_no_triggers(self):
        """No triggers should not require senior review."""
        required, reason = determine_senior_review_required()
        self.assertFalse(required)
        self.assertIsNone(reason)

    def test_single_trigger(self):
        """Single trigger should require senior review."""
        required, reason = determine_senior_review_required(
            security_boundary_changes=True
        )
        self.assertTrue(required)
        self.assertIn("security_authority_boundary_changes", reason)

    def test_multiple_triggers(self):
        """Multiple triggers should all be listed."""
        required, reason = determine_senior_review_required(
            architecture_complex=True,
            security_boundary_changes=True,
            git_deploy_infra_authority_changes=True,
        )
        self.assertTrue(required)
        self.assertIn("complex_new_architecture", reason)
        self.assertIn("security_authority_boundary_changes", reason)
        self.assertIn("git_deployment_infra_authority_changes", reason)

    def test_all_triggers(self):
        """All triggers should be detected."""
        required, reason = determine_senior_review_required(
            architecture_complex=True,
            coupled_control_planes=True,
            security_boundary_changes=True,
            git_deploy_infra_authority_changes=True,
            complex_final_acceptance=True,
            contradictory_results=True,
            orchestrator_uncertainty=True,
        )
        self.assertTrue(required)
        self.assertEqual(reason.count(","), 6)  # 7 items, 6 commas


class TestDetermineVerdict(unittest.TestCase):
    """Test verdict determination."""

    def test_ready_verdict(self):
        """All checks passed should return READY."""
        tests = [TestEvidence(command="pytest", exit_code=0, result="passed")]
        verdict, summary, recommendation = determine_verdict(
            scope_valid=True,
            diff_verified=True,
            tests=tests,
            findings=[],
            external_effects_verified=True,
            correction_round=0,
        )
        self.assertEqual(verdict, ReviewVerdict.READY)
        self.assertIn("satisfied", summary)

    def test_scope_violation_escalates(self):
        """Scope violation should escalate."""
        tests = [TestEvidence(command="pytest", exit_code=0, result="passed")]
        verdict, summary, recommendation = determine_verdict(
            scope_valid=False,
            diff_verified=True,
            tests=tests,
            findings=[],
            external_effects_verified=True,
            correction_round=0,
        )
        self.assertEqual(verdict, ReviewVerdict.ESCALATE)
        self.assertIn("Scope violation", summary)

    def test_unverified_diff_escalates(self):
        """Unverified diff should escalate."""
        tests = [TestEvidence(command="pytest", exit_code=0, result="passed")]
        verdict, summary, recommendation = determine_verdict(
            scope_valid=True,
            diff_verified=False,
            tests=tests,
            findings=[],
            external_effects_verified=True,
            correction_round=0,
        )
        self.assertEqual(verdict, ReviewVerdict.ESCALATE)
        self.assertIn("Diff not verified", summary)

    def test_no_tests_changes_required(self):
        """No tests should require changes."""
        verdict, summary, recommendation = determine_verdict(
            scope_valid=True,
            diff_verified=True,
            tests=[],
            findings=[],
            external_effects_verified=True,
            correction_round=0,
        )
        self.assertEqual(verdict, ReviewVerdict.CHANGES_REQUIRED)
        self.assertIn("Tests missing", summary)

    def test_high_findings_changes_required(self):
        """HIGH findings should require changes."""
        tests = [TestEvidence(command="pytest", exit_code=0, result="passed")]
        findings = [ReviewFinding(severity=Severity.HIGH, description="Issue")]
        verdict, summary, recommendation = determine_verdict(
            scope_valid=True,
            diff_verified=True,
            tests=tests,
            findings=findings,
            external_effects_verified=True,
            correction_round=0,
        )
        self.assertEqual(verdict, ReviewVerdict.CHANGES_REQUIRED)
        self.assertIn("HIGH findings", summary)

    def test_max_corrections_exceeded_escalates(self):
        """Max corrections exceeded should escalate."""
        tests = [TestEvidence(command="pytest", exit_code=0, result="passed")]
        findings = [ReviewFinding(severity=Severity.HIGH, description="Issue")]
        verdict, summary, recommendation = determine_verdict(
            scope_valid=True,
            diff_verified=True,
            tests=tests,
            findings=findings,
            external_effects_verified=True,
            correction_round=2,
            max_corrections=2,
        )
        self.assertEqual(verdict, ReviewVerdict.ESCALATE)
        self.assertIn("Max correction rounds", summary)

    def test_medium_findings_changes_required(self):
        """MEDIUM findings should require changes."""
        tests = [TestEvidence(command="pytest", exit_code=0, result="passed")]
        findings = [ReviewFinding(severity=Severity.MEDIUM, description="Issue")]
        verdict, summary, recommendation = determine_verdict(
            scope_valid=True,
            diff_verified=True,
            tests=tests,
            findings=findings,
            external_effects_verified=True,
            correction_round=0,
        )
        self.assertEqual(verdict, ReviewVerdict.CHANGES_REQUIRED)
        self.assertIn("MEDIUM findings", summary)


class TestRunReviewChecklist(unittest.TestCase):
    """Test complete review checklist."""

    def test_perfect_review(self):
        """Perfect review should return READY."""
        tests = [TestEvidence(command="pytest", exit_code=0, result="passed")]
        result = run_review_checklist(
            scope_valid=True,
            diff_verified=True,
            tests=tests,
            external_effects_verified=True,
            correction_round=0,
        )
        self.assertEqual(result.verdict, ReviewVerdict.READY)
        self.assertTrue(result.scope_valid)
        self.assertTrue(result.diff_verified)
        self.assertFalse(result.senior_review_required)

    def test_review_with_senior_trigger(self):
        """Review with senior trigger should require senior review."""
        tests = [TestEvidence(command="pytest", exit_code=0, result="passed")]
        result = run_review_checklist(
            scope_valid=True,
            diff_verified=True,
            tests=tests,
            external_effects_verified=True,
            correction_round=0,
            security_boundary_changes=True,
        )
        self.assertTrue(result.senior_review_required)
        self.assertIn("security", result.senior_review_reason)

    def test_review_with_high_findings(self):
        """Review with HIGH findings should require changes."""
        tests = [TestEvidence(command="pytest", exit_code=0, result="passed")]
        additional_findings = [
            ReviewFinding(severity=Severity.HIGH, description="Critical issue")
        ]
        result = run_review_checklist(
            scope_valid=True,
            diff_verified=True,
            tests=tests,
            external_effects_verified=True,
            correction_round=0,
            additional_findings=additional_findings,
        )
        self.assertEqual(result.verdict, ReviewVerdict.CHANGES_REQUIRED)
        self.assertGreater(len(result.findings), 0)

    def test_review_with_failed_tests(self):
        """Review with failed tests should require changes."""
        tests = [TestEvidence(command="pytest", exit_code=1, result="failed")]
        result = run_review_checklist(
            scope_valid=True,
            diff_verified=True,
            tests=tests,
            external_effects_verified=True,
            correction_round=0,
        )
        self.assertEqual(result.verdict, ReviewVerdict.CHANGES_REQUIRED)

    def test_review_after_max_corrections(self):
        """Review after max corrections should escalate."""
        tests = [TestEvidence(command="pytest", exit_code=0, result="passed")]
        additional_findings = [
            ReviewFinding(severity=Severity.HIGH, description="Persistent issue")
        ]
        result = run_review_checklist(
            scope_valid=True,
            diff_verified=True,
            tests=tests,
            external_effects_verified=True,
            correction_round=2,
            additional_findings=additional_findings,
        )
        self.assertEqual(result.verdict, ReviewVerdict.ESCALATE)


class TestReviewChecklistResult(unittest.TestCase):
    """Test ReviewChecklistResult serialization."""

    def test_result_to_dict(self):
        """Result should serialize to dict."""
        tests = [TestEvidence(command="pytest", exit_code=0, result="passed")]
        findings = [ReviewFinding(severity=Severity.LOW, description="Minor issue")]
        result = ReviewChecklistResult(
            verdict=ReviewVerdict.READY,
            summary="All checks passed",
            scope_valid=True,
            diff_verified=True,
            tests=tests,
            findings=findings,
            external_effects_verified=True,
            senior_review_required=False,
            senior_review_reason=None,
            correction_round=0,
            recommendation="Ready for deployment",
        )
        d = result.to_dict()
        self.assertEqual(d["verdict"], "ready")
        self.assertEqual(d["summary"], "All checks passed")
        self.assertTrue(d["scope_valid"])
        self.assertTrue(d["diff_verified"])
        self.assertEqual(len(d["tests"]), 1)
        self.assertEqual(len(d["findings"]), 1)
        self.assertFalse(d["senior_review_required"])
        self.assertEqual(d["correction_round"], 0)
        self.assertEqual(d["max_corrections"], 2)


class TestCorePolicyCompliance(unittest.TestCase):
    """Test Core Policy compliance."""

    def test_max_2_correction_rounds(self):
        """Core policy: max 2 correction rounds."""
        tests = [TestEvidence(command="pytest", exit_code=0, result="passed")]
        result = run_review_checklist(
            scope_valid=True,
            diff_verified=True,
            tests=tests,
            external_effects_verified=True,
            correction_round=0,
        )
        self.assertEqual(result.max_corrections, 2)

    def test_fail_closed_on_missing_evidence(self):
        """Core policy: fail-closed on missing evidence."""
        result = run_review_checklist(
            scope_valid=True,
            diff_verified=True,
            tests=[],  # No test evidence
            external_effects_verified=True,
            correction_round=0,
        )
        self.assertEqual(result.verdict, ReviewVerdict.CHANGES_REQUIRED)

    def test_senior_review_for_security_changes(self):
        """Core policy: senior review required for security boundary changes."""
        tests = [TestEvidence(command="pytest", exit_code=0, result="passed")]
        result = run_review_checklist(
            scope_valid=True,
            diff_verified=True,
            tests=tests,
            external_effects_verified=True,
            correction_round=0,
            security_boundary_changes=True,
        )
        self.assertTrue(result.senior_review_required)

    def test_senior_review_for_git_authority_changes(self):
        """Core policy: senior review required for git/deploy/infra authority changes."""
        tests = [TestEvidence(command="pytest", exit_code=0, result="passed")]
        result = run_review_checklist(
            scope_valid=True,
            diff_verified=True,
            tests=tests,
            external_effects_verified=True,
            correction_round=0,
            git_deploy_infra_authority_changes=True,
        )
        self.assertTrue(result.senior_review_required)


if __name__ == "__main__":
    unittest.main()
