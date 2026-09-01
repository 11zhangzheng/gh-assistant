from __future__ import annotations

from pathlib import Path

from gh_assistant.github_client import GitHubClient, GitHubError
from gh_assistant.workspace import (
    WorktreeManager,
    commit_changes,
    current_head,
    git_diff,
    git_status,
    snapshot_hash,
)


class Response:
    def __init__(self, value, *, status=200, headers=None):
        self.value = value
        self.status_code = status
        self.ok = 200 <= status < 300
        self.headers = headers or {}
        self.text = "error" if not self.ok else ""
        self.content = b"x" if value is not None else b""

    def json(self):
        return self.value


class Session:
    def __init__(self, responses):
        self.headers = {}
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def test_github_pagination_filters_pull_requests():
    issues = [
        {"number": 1, "title": "issue"},
        {"number": 2, "title": "pr", "pull_request": {}},
    ]
    session = Session([Response(issues)])
    client = GitHubClient("token", session=session)
    assert [item["number"] for item in client.list_issues("o/r", limit=2)] == [1]
    assert session.calls[0][2]["params"]["per_page"] == 4


def test_github_draft_pr_is_idempotent_by_head_branch():
    existing = {
        "number": 4,
        "body": "<!-- gh-assistant:run_1 -->",
        "head": {"ref": "gha/issue-1"},
        "html_url": "https://example/pr/4",
    }
    session = Session([Response([existing])])
    client = GitHubClient("token", session=session)
    pull, created = client.ensure_draft_pr(
        repo="owner/repo",
        head_branch="gha/issue-1",
        base_branch="main",
        title="fix",
        body="body",
        run_id="run_1",
    )
    assert pull["number"] == 4
    assert created is False
    assert len(session.calls) == 1


def test_worktree_diff_hash_and_commit(git_repo: Path, tmp_path: Path):
    manager = WorktreeManager(tmp_path / "agent-state")
    worktree = manager.create(
        repo_path=git_repo,
        repo_slug="owner/repo",
        issue_number=1,
        run_id="run_abc",
        default_branch="main",
    )
    original_head = current_head(worktree.path)
    target = worktree.path / "bug.py"
    target.write_text(
        "def add_one(value: int) -> int:\n    return value + 1\n", encoding="utf-8"
    )
    assert "bug.py" in git_status(worktree.path)
    assert "return value + 1" in git_diff(worktree.path)
    first_hash = snapshot_hash(worktree.path)
    commit = commit_changes(worktree.path, issue_number=1, run_id="run_abc")
    assert commit != original_head
    assert current_head(worktree.path) == commit
    assert git_status(worktree.path) == "(clean)"
    assert len(first_hash) == 64

