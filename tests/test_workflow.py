from __future__ import annotations

from pathlib import Path

from gh_assistant.config import Settings
from gh_assistant.contracts import ModelResponse, TextPart, ToolCallPart
from gh_assistant.providers import ScriptedBackend
from gh_assistant.report import generate_report
from gh_assistant.state import StateStore
from gh_assistant.workflow import SolveWorkflow


class FakeGitHub:
    def __init__(self):
        self.pr_calls = 0

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
    assert len(pushed) == 1
    assert github.pr_calls == 1
    again = workflow.resume(run["id"])
    assert again["status"] == "published"
    assert len(pushed) == 1
    assert github.pr_calls == 1

