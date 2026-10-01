from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from gh_assistant.config import Settings
from gh_assistant.contracts import ModelResponse, TextPart, ToolCallPart
from gh_assistant.providers import ScriptedBackend
from gh_assistant.report import generate_report
from gh_assistant.state import StateStore
from gh_assistant.workflow import SolveWorkflow, _pull_request_body


class FakeGitHub:
    def __init__(self):
        self.pr_calls = 0
        self.pr_kwargs = None

    def get_repo(self, repo):
        return {
            "full_name": repo,
            "default_branch": "main",
            "description": "fixture",
            "private": True,
        }

    def get_issue(self, repo, number):
        return {
            "number": number,
            "title": "add_one returns the input unchanged",
            "body": "add_one(1) must return 2 and the regression test must pass.",
            "state": "open",
            "labels": [{"name": "bug"}],
            "comments_data": [],
        }

    def ensure_draft_pr(self, **kwargs):
        self.pr_calls += 1
        self.pr_kwargs = kwargs
        return (
            {
                "number": 9,
                "html_url": "https://example.invalid/pr/9",
                "draft": True,
                "head": {"ref": kwargs["head_branch"]},
            },
            self.pr_calls == 1,
        )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        provider="scripted",
        model="scripted",
        github_token="test-token",
        state_dir=tmp_path / "state",
        executor="local",
        interactive=True,
    )


def _happy_script():
    return [
        ModelResponse(
            [
                ToolCallPart(
                    "plan",
                    "submit_plan",
                    {"summary": "Fix return value", "tasks": ["Inspect and patch bug.py", "Run tests"]},
                )
            ]
        ),
        ModelResponse(
            [
                ToolCallPart(
                    "patch",
                    "apply_patch",
                    {
                        "path": "bug.py",
                        "old_text": "    return value\n",
                        "new_text": "    return value + 1\n",
                    },
                )
            ]
        ),
        ModelResponse(
            [ToolCallPart("finish", "finish_task", {"summary": "Correct add_one"})]
        ),
        ModelResponse(
            [
                ToolCallPart(
                    "review",
                    "submit_review",
                    {"verdict": "approve", "summary": "Correct and covered", "findings": []},
                )
            ]
        ),
    ]


def test_full_workflow_completes_local_patch_with_independent_review(
    tmp_path: Path, git_repo: Path
):
    settings = _settings(tmp_path)
    state = StateStore(settings.state_dir)
    workflow = SolveWorkflow(
        settings,
        state=state,
        backend=ScriptedBackend(_happy_script()),
        github=FakeGitHub(),
        approval_callback=lambda approval: True,
        profile="full",
        publish=False,
    )
    run = workflow.start(repo="owner/repo", issue_number=1, repo_path=git_repo)
    assert run["status"] == "completed_local"
    assert run["phase"] == "done"
    assert run["result"]["verification"]["passed"] is True
    assert run["result"]["review"]["verdict"] == "approve"
    assert "return value + 1" in Path(run["worktree_path"], "bug.py").read_text(encoding="utf-8")
    assert "return value\n" in (git_repo / "bug.py").read_text(encoding="utf-8")
    _, html_report = generate_report(state, run["id"])
    assert html_report.exists()


def test_verification_failure_returns_to_main_agent_for_repair(
    tmp_path: Path, git_repo: Path
):
    script = [
        ModelResponse(
            [ToolCallPart("plan", "submit_plan", {"summary": "fix", "tasks": ["patch"]})]
        ),
        ModelResponse(
            [
                ToolCallPart(
                    "bad-patch",
                    "apply_patch",
                    {
                        "path": "bug.py",
                        "old_text": "    return value\n",
                        "new_text": "    return value + 2\n",
                    },
                )
            ]
        ),
        ModelResponse([ToolCallPart("finish1", "finish_task", {"summary": "first"})]),
        ModelResponse(
            [
                ToolCallPart(
                    "repair",
                    "apply_patch",
                    {
                        "path": "bug.py",
                        "old_text": "    return value + 2\n",
                        "new_text": "    return value + 1\n",
                    },
                )
            ]
        ),
        ModelResponse([ToolCallPart("finish2", "finish_task", {"summary": "repaired"})]),
        ModelResponse(
            [ToolCallPart("review", "submit_review", {"verdict": "approve", "summary": "ok", "findings": []})]
        ),
    ]
    settings = _settings(tmp_path)
    state = StateStore(settings.state_dir)
    run = SolveWorkflow(
        settings,
        state=state,
        backend=ScriptedBackend(script),
        github=FakeGitHub(),
        approval_callback=lambda approval: True,
        publish=False,
    ).start(repo="owner/repo", issue_number=1, repo_path=git_repo)
    assert run["status"] == "completed_local"
    assert run["result"]["workflow"]["verification_repairs"] == 1
    assert sum(event["event_type"] == "verification_command" for event in state.events(run["id"])) == 2


def test_publish_waits_for_hash_bound_approval_and_resume_is_idempotent(
    tmp_path: Path, git_repo: Path, monkeypatch
):
    settings = _settings(tmp_path)
    state = StateStore(settings.state_dir)
    github = FakeGitHub()

    def approvals(approval):
        if approval["action"] == "local_executor":
            return True
        return None

    workflow = SolveWorkflow(
        settings,
        state=state,
        backend=ScriptedBackend(_happy_script()),
        github=github,
        approval_callback=approvals,
        publish=True,
    )
    run = workflow.start(repo="owner/repo", issue_number=1, repo_path=git_repo)
    assert run["status"] == "waiting_approval"
    pending = state.list_approvals(run_id=run["id"], status="pending")
    publish = next(item for item in pending if item["action"] == "publish_draft_pr")
    state.decide_approval(publish["id"], "approved")
    pushed = []
    monkeypatch.setattr(
        "gh_assistant.workflow.push_branch",
        lambda worktree, **kwargs: pushed.append((worktree, kwargs)) or kwargs["branch"],
    )
    completed = workflow.resume(run["id"])
    assert completed["status"] == "published"
    assert completed["result"]["pull_request"]["number"] == 9
    assert completed["result"]["outcome"] == "CANDIDATE_FIX"
    assert "Human verification required" in github.pr_kwargs["body"]
    assert github.pr_kwargs["title"].startswith("[Candidate]")
    assert len(pushed) == 1
    assert github.pr_calls == 1
    again = workflow.resume(run["id"])
    assert again["status"] == "published"
    assert len(pushed) == 1
    assert github.pr_calls == 1


def test_no_repository_validation_remains_candidate(tmp_path: Path, git_repo: Path):
    (git_repo / "pyproject.toml").unlink()
    (git_repo / "gh-assistant.yaml").unlink()
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "-c", "user.name=tests", "-c", "user.email=tests@example.invalid", "commit", "-m", "remove config"], cwd=git_repo, check=True, capture_output=True)
    settings = _settings(tmp_path)
    state = StateStore(settings.state_dir)
    run = SolveWorkflow(
        settings, state=state, backend=ScriptedBackend(_happy_script()),
        github=FakeGitHub(), approval_callback=lambda approval: True, publish=False,
    ).start(repo="owner/repo", issue_number=1, repo_path=git_repo)
    assert run["result"]["verification"]["unverified"] is True
    assert run["result"]["outcome"] == "CANDIDATE_FIX"
    assert run["result"]["evidence"]["repository_checks"][0]["status"] == "NOT_RUN"
    json_report, _ = generate_report(state, run["id"])
    assert "CANDIDATE_FIX" in json_report.read_text(encoding="utf-8")
    _, html_report = generate_report(state, run["id"])
    assert "<td>NOT_RUN</td>" in html_report.read_text(encoding="utf-8")
    body = _pull_request_body(run, run["result"])
    assert "No verification command configured" in body
    assert "Human verification required" in body


def test_observed_before_failure_after_pass_and_review_yields_verified(tmp_path: Path, git_repo: Path):
    script = _happy_script()
    script[0].parts[0].arguments.update({
        "expected_behavior": "add_one(1) returns 2",
        "reproduction_command": [sys.executable, "-c", "from bug import add_one; assert add_one(1) == 2"],
        "failure_signature": "AssertionError",
        "planned_files": ["bug.py"],
    })
    settings = _settings(tmp_path)
    run = SolveWorkflow(
        settings, backend=ScriptedBackend(script), github=FakeGitHub(),
        approval_callback=lambda approval: True, publish=False,
    ).start(repo="owner/repo", issue_number=1, repo_path=git_repo)
    assert run["result"]["outcome"] == "VERIFIED_FIX"
    regression = run["result"]["evidence"]["regression"]
    assert regression["before"]["status"] == "FAIL"
    assert regression["after"]["status"] == "PASS"


def test_unavailable_repository_command_is_candidate_without_repair_loop(tmp_path: Path, git_repo: Path):
    (git_repo / "gh-assistant.yaml").write_text(
        'version: 1\nverify:\n  - ["missing-verifier-command-xyz"]\n', encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "-c", "user.name=tests", "-c", "user.email=tests@example.invalid", "commit", "-m", "unavailable verifier"], cwd=git_repo, check=True, capture_output=True)
    settings = _settings(tmp_path)
    run = SolveWorkflow(
        settings, backend=ScriptedBackend(_happy_script()), github=FakeGitHub(),
        approval_callback=lambda approval: True, publish=False,
    ).start(repo="owner/repo", issue_number=1, repo_path=git_repo)
    assert run["result"]["outcome"] == "CANDIDATE_FIX"
    assert run["result"]["evidence"]["repository_checks"][0]["status"] == "UNAVAILABLE"
    assert run["result"]["workflow"]["verification_repairs"] == 0


def test_agent_can_abstain_without_publishing(tmp_path: Path, git_repo: Path):
    settings = _settings(tmp_path)
    github = FakeGitHub()
    workflow = SolveWorkflow(
        settings,
        backend=ScriptedBackend([ModelResponse([ToolCallPart("stop", "abstain", {
            "reason": "Expected behavior is ambiguous", "confirmed": ["Issue is open"],
            "missing": ["Expected output"], "next_step": "Ask reporter for example",
        })])]),
        github=github, approval_callback=lambda approval: True,
    )
    run = workflow.start(repo="owner/repo", issue_number=1, repo_path=git_repo)
    assert run["status"] == "abstained"
    assert run["phase"] == "done"
    assert run["result"]["outcome"] == "ABSTAIN"
    assert run["result"]["abstain"]["next_step"] == "Ask reporter for example"
    assert github.pr_calls == 0
    _, html_report = generate_report(workflow.state, run["id"])
    html_text = html_report.read_text(encoding="utf-8")
    assert "<b>Next step:</b> Ask reporter for example" in html_text
    assert "<b>Missing:</b> Expected output" in html_text


def test_patch_change_after_evidence_decision_prevents_publication(tmp_path: Path, git_repo: Path):
    settings = _settings(tmp_path)
    github = FakeGitHub()
    workflow = SolveWorkflow(
        settings, backend=ScriptedBackend(_happy_script()), github=github,
        approval_callback=lambda approval: True, publish=True,
    )
    original_decide = workflow._decide_outcome

    def change_after_decision(run_id, worktree):
        outcome = original_decide(run_id, worktree)
        (worktree.path / "bug.py").write_text("def add_one(value):\n    return 999\n", encoding="utf-8")
        return outcome

    workflow._decide_outcome = change_after_decision
    run = workflow.start(repo="owner/repo", issue_number=1, repo_path=git_repo)
    assert run["status"] == "abstained"
    assert run["result"]["outcome"] == "ABSTAIN"
    assert "changed after verification" in run["result"]["abstain"]["reason"]
    assert github.pr_calls == 0
