from __future__ import annotations

from gh_assistant.evidence import Outcome, decide_outcome, empty_evidence
from gh_assistant.config import Settings


def complete_evidence():
    evidence = empty_evidence()
    evidence["expected_behavior"] = "add_one(1) returns 2"
    evidence["reproduction"].update(status="PASS", command=["python", "-m", "pytest", "-q", "tests/test_bug.py"])
    evidence["regression"].update(
        command=["python", "-m", "pytest", "-q", "tests/test_bug.py"],
        before={"status": "FAIL", "exit_code": 1, "output": "assert 1 == 2"},
        after={"status": "PASS", "exit_code": 0, "output": "1 passed"},
    )
    evidence["repository_checks"] = [{"kind": "test", "status": "PASS", "command": ["python", "-m", "pytest", "-q"]}]
    evidence["review"] = {"status": "PASS", "blockers": [], "warnings": [], "info": []}
    evidence["fix_scope"] = {"planned_files": ["bug.py"], "actual_files": ["bug.py"], "out_of_scope": [], "violation": False}
    return evidence


def test_full_local_evidence_is_verified_with_explicit_local_policy():
    decision = decide_outcome(complete_evidence(), has_patch=True, allow_local_verified=True)
    assert decision.outcome == Outcome.VERIFIED_FIX
    assert decision.reasons == []


def test_missing_repository_verification_is_candidate():
    evidence = complete_evidence()
    evidence["repository_checks"] = []
    decision = decide_outcome(evidence, has_patch=True, allow_local_verified=True)
    assert decision.outcome == Outcome.CANDIDATE_FIX
    assert "Repository verification is missing" in decision.reasons


def test_no_reproduction_is_abstain():
    evidence = complete_evidence()
    evidence["reproduction"]["status"] = "FAIL"
    decision = decide_outcome(evidence, has_patch=True, allow_local_verified=True)
    assert decision.outcome == Outcome.ABSTAIN


def test_review_blocker_prevents_verified():
    evidence = complete_evidence()
    evidence["review"]["blockers"] = ["Incorrect edge case"]
    decision = decide_outcome(evidence, has_patch=True, allow_local_verified=True)
    assert decision.outcome == Outcome.ABSTAIN


def test_scope_violation_prevents_verified():
    evidence = complete_evidence()
    evidence["fix_scope"]["violation"] = True
    decision = decide_outcome(evidence, has_patch=True, allow_local_verified=True)
    assert decision.outcome == Outcome.CANDIDATE_FIX


def test_ci_not_run_requires_explicit_local_policy():
    decision = decide_outcome(complete_evidence(), has_patch=True, allow_local_verified=False)
    assert decision.outcome == Outcome.CANDIDATE_FIX
    assert "CI has not passed" in decision.reasons


def test_missing_regression_before_fails_closed():
    evidence = complete_evidence()
    evidence["regression"]["before"]["status"] = "NOT_RUN"
    assert decide_outcome(evidence, has_patch=True, allow_local_verified=True).outcome == Outcome.CANDIDATE_FIX


def test_local_only_verified_policy_can_be_disabled_by_host_env(monkeypatch):
    monkeypatch.setenv("GHA_ALLOW_LOCAL_VERIFIED", "false")
    policy_value = Settings.from_env().allow_local_verified
    assert policy_value is False
