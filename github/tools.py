#!/usr/bin/env python3
"""
github/tools.py — s02: GitHub tool definitions + handlers.

Each handler wraps GitHubClient and returns a plain-text summary
(the loop feeds it back to the model as a tool_result).

    from github.api import GitHubClient
    from github.tools import build_github_tools
    registry = build_github_tools(GitHubClient())
"""
from __future__ import annotations

from core.tools import ToolRegistry
from github.api import GitHubApiError, GitHubClient


def _err(e: Exception) -> str:
    return f"Error: {e}"


def build_github_tools(client: GitHubClient) -> ToolRegistry:
    reg = ToolRegistry()

    # ── repo ───────────────────────────────────────────────
    def repo_info(repo: str) -> str:
        try:
            r = client.get_repo(repo)
            return (f"{r['full_name']}: {r.get('description') or '(no description)'}\n"
                    f"default_branch={r.get('default_branch')} "
                    f"open_issues={r.get('open_issues_count')} "
                    f"stars={r.get('stargazers_count')} language={r.get('language')}")
        except GitHubApiError as e:
            return _err(e)
    reg.register("repo_info", "Get basic metadata about a GitHub repository.",
                 {"repo": {"type": "string", "description": "owner/name"}},
                 ["repo"], repo_info)

    # ── issues (read) ──────────────────────────────────────
    def list_issues(repo: str, state: str = "open", limit: int = 10) -> str:
        try:
            issues = client.list_issues(repo, state=state, limit=limit)
            if not issues:
                return "(no issues)"
            lines = []
            for i in issues:
                labels = ", ".join(l["name"] for l in i["labels"]) or "unlabeled"
                lines.append(f"#{i['number']} [{labels}] {i['title']}")
            return "\n".join(lines)
        except GitHubApiError as e:
            return _err(e)
    reg.register("list_issues",
                 "List issues in a repo. PRs are excluded. state: open/closed/all.",
                 {"repo": {"type": "string"},
                  "state": {"type": "string", "enum": ["open", "closed", "all"]},
                  "limit": {"type": "integer"}},
                 ["repo"], list_issues)

    def get_issue(repo: str, number: int) -> str:
        try:
            i = client.get_issue(repo, number)
            comments = client.list_comments(repo, number)
            parts = [f"#{i['number']} [{i['state']}] {i['title']}\n{i['body'] or ''}"]
            if comments:
                parts.append("── comments ──")
                parts.extend(f"@{c['user']['login']}: {c['body']}"
                             for c in comments[:10])
            return "\n".join(parts)
        except GitHubApiError as e:
            return _err(e)
    reg.register("get_issue", "Get a full issue (body + up to 10 comments).",
                 {"repo": {"type": "string"}, "number": {"type": "integer"}},
                 ["repo", "number"], get_issue)

    def list_labels(repo: str) -> str:
        try:
            labels = client.list_labels(repo)
            return "\n".join(f"{l['name']} — {l.get('description') or ''}"
                             for l in labels) or "(no labels)"
        except GitHubApiError as e:
            return _err(e)
    reg.register("list_labels", "List the repo's existing labels.",
                 {"repo": {"type": "string"}},
                 ["repo"], list_labels)

    # ── issue writes (permission-gated) ────────────────────
    def add_labels(repo: str, issue_number: int, labels: list[str]) -> str:
        try:
            added = client.add_labels(repo, issue_number, labels)
            return (f"Added labels to #{issue_number}: "
                    f"{', '.join(l['name'] for l in added)}")
        except GitHubApiError as e:
            return _err(e)
    reg.register("add_labels", "Add labels to an issue. Writes to the repo.",
                 {"repo": {"type": "string"},
                  "issue_number": {"type": "integer"},
                  "labels": {"type": "array", "items": {"type": "string"}}},
                 ["repo", "issue_number", "labels"], add_labels)

    def comment_on_issue(repo: str, issue_number: int, body: str) -> str:
        try:
            c = client.comment_on_issue(repo, issue_number, body)
            return f"Commented on #{issue_number} ({c.get('html_url')})"
        except GitHubApiError as e:
            return _err(e)
    reg.register("comment_on_issue", "Post a comment on an issue. Writes to the repo.",
                 {"repo": {"type": "string"},
                  "issue_number": {"type": "integer"},
                  "body": {"type": "string"}},
                 ["repo", "issue_number", "body"], comment_on_issue)

    def close_issue(repo: str, issue_number: int) -> str:
        try:
            i = client.close_issue(repo, issue_number)
            return f"Closed #{issue_number}: {i['title']}"
        except GitHubApiError as e:
            return _err(e)
    reg.register("close_issue", "Close an issue. High-risk: changes repo state.",
                 {"repo": {"type": "string"}, "issue_number": {"type": "integer"}},
                 ["repo", "issue_number"], close_issue)

    return reg
