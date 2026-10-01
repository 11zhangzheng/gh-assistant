"""Structured repair evidence and the single outcome decision gate."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Outcome(StrEnum):
    VERIFIED_FIX = "VERIFIED_FIX"
    CANDIDATE_FIX = "CANDIDATE_FIX"
    ABSTAIN = "ABSTAIN"


@dataclass(slots=True)
class OutcomeDecision:
    outcome: Outcome
    reasons: list[str]


def empty_evidence() -> dict[str, Any]:
    """Return a JSON-serializable contract; unknown is never equivalent to pass."""
    return {
        "expected_behavior": "",
        "reproduction": {
            "status": "NOT_RUN", "command": [], "before": check_result("NOT_RUN"),
            "failure_signature": "", "stable": None, "reason": "",
        },
        "root_cause": {"summary": "", "locations": [], "basis": [], "confidence": None},
        "fix_scope": {"planned_files": [], "actual_files": [], "out_of_scope": [], "violation": False},
        "patch_hash": "",
        "regression": {"command": [], "before": check_result("NOT_RUN"), "after": check_result("NOT_RUN")},
        "repository_checks": [],
        "review": {"status": "NOT_RUN", "blockers": [], "warnings": [], "info": []},
        "ci": {"status": "NOT_RUN", "checks": []},
        "unverified_claims": [],
    }


def check_result(status: str, *, exit_code: int | None = None, output: str = "") -> dict[str, Any]:
    if status not in {"PASS", "FAIL", "NOT_RUN", "UNAVAILABLE"}:
        raise ValueError(f"Invalid check status: {status}")
    return {"status": status, "exit_code": exit_code, "output": output[:2_000]}


def decide_outcome(
    evidence: dict[str, Any], *, has_patch: bool, allow_local_verified: bool,
    abstain_reason: str = "",
) -> OutcomeDecision:
    """Use observed checks for verification; model confidence is never a gate input."""
    if abstain_reason:
        return OutcomeDecision(Outcome.ABSTAIN, [abstain_reason])
    if not has_patch:
        return OutcomeDecision(Outcome.ABSTAIN, ["No patch was produced"])
    reproduction = evidence.get("reproduction", {})
    if reproduction.get("status") == "FAIL":
        return OutcomeDecision(Outcome.ABSTAIN, ["The reported bug could not be reproduced"])
    review = evidence.get("review", {})
    if review.get("blockers") or review.get("status") == "FAIL":
        return OutcomeDecision(Outcome.ABSTAIN, ["Independent review found a blocker"])
    checks = evidence.get("repository_checks", [])
    if any(item.get("status") == "FAIL" for item in checks):
        return OutcomeDecision(Outcome.ABSTAIN, ["Repository verification failed"])

    reasons = []
    if not evidence.get("expected_behavior"):
        reasons.append("Expected behavior is not documented")
    if reproduction.get("status") != "PASS":
        reasons.append("Reproduction evidence is missing")
    regression = evidence.get("regression", {})
    if not regression.get("command") or regression.get("before", {}).get("status") != "FAIL" or regression.get("after", {}).get("status") != "PASS":
        reasons.append("Before/after targeted regression evidence is incomplete")
    if not checks:
        reasons.append("Repository verification is missing")
    elif any(item.get("status") != "PASS" for item in checks):
        reasons.append("Repository verification is incomplete")
    if review.get("status") != "PASS":
        reasons.append("Independent review is missing")
    scope = evidence.get("fix_scope", {})
    if scope.get("violation"):
        reasons.append("Patch changes files outside the planned scope")
    if not scope.get("planned_files") or not scope.get("actual_files"):
        reasons.append("Fix scope is not documented")
    ci = evidence.get("ci", {})
    if ci.get("status") != "PASS" and not allow_local_verified:
        reasons.append("CI has not passed")
    if ci.get("status") == "FAIL":
        reasons.append("CI failed")
    return OutcomeDecision(
        Outcome.CANDIDATE_FIX if reasons else Outcome.VERIFIED_FIX, reasons
    )
