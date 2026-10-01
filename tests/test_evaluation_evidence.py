from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from gh_assistant.config import Settings
from gh_assistant.contracts import ModelResponse, ToolCallPart
from gh_assistant.evaluation import _load_real_tasks, run_benchmark, summarize_results
from gh_assistant.providers import ScriptedBackend


def test_evaluation_summarizes_outcomes_evidence_and_nullable_human_cost():
    rows = [
        {
            "id": "fixed", "hidden_passed": True, "outcome": "VERIFIED_FIX",
            "duration_seconds": 12.5,
            "evidence": {
                "reproduction": {"status": "PASS"},
                "regression": {"before": {"status": "FAIL"}, "after": {"status": "PASS"}},
                "repository_checks": [{"status": "PASS"}],
                "fix_scope": {"violation": False}, "review": {"blockers": []},
            },
            "usage": {"model_calls": 3, "input_tokens": 100, "output_tokens": 20,
                      "estimated_cost_usd": None, "tool_calls": 4},
            "human": {"accepted": True, "maintainer_review_minutes": 5,
                      "manual_debug_minutes": 2, "maintainer_interventions": 1,
                      "requested_revisions": 0, "incorrect_fix": False},
        },
        {
            "id": "candidate", "hidden_passed": False, "outcome": "CANDIDATE_FIX",
            "duration_seconds": 7.5,
            "evidence": {"reproduction": {"status": "NOT_RUN"}, "regression": {},
                         "repository_checks": [], "fix_scope": {"violation": True},
                         "review": {"blockers": ["missing tests"]}},
            "usage": {"model_calls": 2, "input_tokens": 50, "output_tokens": 10,
                      "estimated_cost_usd": None, "tool_calls": 3},
            "human": {"accepted": False, "maintainer_review_minutes": 4,
                      "manual_debug_minutes": 1, "maintainer_interventions": 2,
                      "requested_revisions": 1},
        },
    ]
    summary = summarize_results(rows, "full")
    assert summary["solve_rate"] == 0.5
    assert summary["verified_fix_rate"] == 0.5
    assert summary["candidate_fix_rate"] == 0.5
    assert summary["incorrect_fix_rate"] == 0.0
    assert summary["incorrect_fix_annotated"] == 1
    assert summary["reproduction_success_rate"] == 0.5
    assert summary["regression_evidence_rate"] == 0.5
    assert summary["scope_violation_rate"] == 0.5
    assert summary["maintainer_intervention_minutes_per_accepted_fix"] == 12.0
    assert summary["estimated_model_cost_usd"] is None
    assert summary["model_calls"] == 5


def test_human_metrics_are_unknown_when_not_annotated():
    summary = summarize_results([{
        "id": "one", "hidden_passed": None, "outcome": "ABSTAIN",
        "duration_seconds": 1, "evidence": {}, "usage": {}, "human": {},
    }], "full")
    assert summary["solve_rate"] is None
    assert summary["incorrect_fix_rate"] is None
    assert summary["maintainer_intervention_minutes_per_accepted_fix"] is None
    assert summary["maintainer_interventions"] is None
    assert summary["abstain_rate"] == 1.0


def test_real_task_schema_loads_local_snapshot_and_rejects_missing_provenance(tmp_path: Path):
    task = tmp_path / "task-a"
    task.mkdir()
    (task / "repository").mkdir()
    (task / "repository" / "bug.py").write_text("value = 1\n", encoding="utf-8")
    (task / "issue.md").write_text("Actual output differs from expected.\n", encoding="utf-8")
    (task / "task.yaml").write_text(
        "id: task-a\nrepo: fixture/task-a\nissue_number: 1\nissue_url: null\n"
        "base_commit: null\nlanguage: python\nissue_type: bug\n"
        "metadata: {source: high_realism_fixture, difficulty: easy}\n"
        "reproduction: {available: true, command: [python, -c, pass]}\n"
        "verification: {targeted: [python, -c, pass], repository: []}\n",
        encoding="utf-8",
    )
    loaded = _load_real_tasks(tmp_path)
    assert loaded[0]["issue"]["body"] == "Actual output differs from expected.\n"
    assert loaded[0]["source_dir"] == task / "repository"
    assert loaded[0]["metadata"]["source"] == "high_realism_fixture"
    (task / "task.yaml").write_text((task / "task.yaml").read_text(encoding="utf-8").replace("high_realism_fixture", "github_issue"), encoding="utf-8")
    with pytest.raises(ValueError, match="issue_url|base_commit"):
        _load_real_tasks(tmp_path)


def test_bundled_examples_are_explicitly_high_realism_not_real_github_issues():
    root = Path(__file__).resolve().parents[1] / "benchmarks" / "real_issues"
    tasks = _load_real_tasks(root)
    assert len(tasks) == 2
    assert all(task["metadata"]["source"] == "high_realism_fixture" for task in tasks)
    assert all(task["base_commit"] is None for task in tasks)
    assert all(task["verification"]["hidden"] for task in tasks)


def test_real_task_runner_executes_local_snapshot_and_hidden_judge(tmp_path: Path):
    source = Path(__file__).resolve().parents[1] / "benchmarks" / "real_issues" / "retry-after-header"
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    shutil.copytree(source, tasks_dir / source.name)
    script = [
        ModelResponse([ToolCallPart("plan", "submit_plan", {
            "summary": "Normalize header lookup", "tasks": ["Patch retry_policy.py"],
            "expected_behavior": "Header names are case insensitive",
            "reproduction_command": ["python", "-m", "pytest", "-q", "tests/test_regression.py"],
            "failure_signature": "FAILED", "planned_files": ["retry_policy.py"],
        })]),
        ModelResponse([ToolCallPart("patch", "apply_patch", {
            "path": "retry_policy.py",
            "old_text": 'value = headers.get("Retry-After")',
            "new_text": 'value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)',
        })]),
        ModelResponse([ToolCallPart("finish", "finish_task", {"summary": "Case-insensitive lookup"})]),
        ModelResponse([ToolCallPart("review", "submit_review", {"verdict": "approve", "summary": "Correct", "findings": []})]),
    ]
    result = run_benchmark(
        Settings(provider="scripted", model="test", state_dir=tmp_path / "state", executor="local"),
        manifest_path=tasks_dir, profile="full", output_dir=tmp_path / "out",
        approval_callback=lambda approval: True, backend=ScriptedBackend(script),
    )
    assert result["summary"]["benchmark_kind"] == "offline_bug_tasks"
    assert result["summary"]["hidden_judged"] == 1
    assert result["summary"]["solve_rate"] == 1.0
    assert result["summary"]["verified_fix_rate"] == 1.0
    assert result["results"][0]["source"] == "high_realism_fixture"
