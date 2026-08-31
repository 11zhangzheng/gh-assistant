#!/usr/bin/env python3
"""
github/api.py — GitHubClient: thin REST wrapper over the GitHub API.

Every gh-assistant GitHub tool goes through this client. Read methods
return parsed dicts; write methods return the created/updated object.

Auth: GITHUB_TOKEN in .env (fine-grained PAT with Issues read+write on the repos you use).
Docs: https://docs.github.com/rest/issues/issues
"""
from __future__ import annotations

import os

import requests


class GitHubApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class GitHubClient:
    BASE_URL = "https://api.github.com"

    def __init__(self, token: str | None = None, base_url: str = BASE_URL):
        token = token or os.getenv("GITHUB_TOKEN")
        if not token:
            raise GitHubApiError(
                "GITHUB_TOKEN is not set. Add it to .env (or export it).")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        self.base_url = base_url

    # ── low-level ──────────────────────────────────────────
    def _get(self, path: str, params: dict | None = None):
        r = self.session.get(f"{self.base_url}{path}", params=params, timeout=30)
        return self._handle(r)

    def _post(self, path: str, payload: dict | None = None):
        r = self.session.post(f"{self.base_url}{path}", json=payload, timeout=30)
        return self._handle(r)

    def _patch(self, path: str, payload: dict | None = None):
        r = self.session.patch(f"{self.base_url}{path}", json=payload, timeout=30)
        return self._handle(r)

    def _handle(self, r: requests.Response):
        if not r.ok:
            raise GitHubApiError(
                f"GitHub API {r.request.method} {r.request.url} → {r.status_code}: "
                f"{r.text[:300]}", status=r.status_code)
        return r.json() if r.content else {}

    # ── repo ───────────────────────────────────────────────
    def get_repo(self, repo: str) -> dict:
        return self._get(f"/repos/{repo}")

    # ── issues ─────────────────────────────────────────────
    def list_issues(self, repo: str, state: str = "open", limit: int = 10) -> list[dict]:
        issues = self._get(f"/repos/{repo}/issues",
                           params={"state": state, "per_page": min(limit, 100)})
        # The issues endpoint also returns pull requests — filter them out.
        return [i for i in issues if "pull_request" not in i]

    def get_issue(self, repo: str, number: int) -> dict:
        return self._get(f"/repos/{repo}/issues/{number}")

    def list_comments(self, repo: str, number: int) -> list[dict]:
        return self._get(f"/repos/{repo}/issues/{number}/comments")

    def list_labels(self, repo: str) -> list[dict]:
        return self._get(f"/repos/{repo}/labels")

    # ── issue writes ───────────────────────────────────────
    def add_labels(self, repo: str, issue_number: int, labels: list[str]) -> list[dict]:
        return self._post(f"/repos/{repo}/issues/{issue_number}/labels",
                          {"labels": labels})

    def comment_on_issue(self, repo: str, issue_number: int, body: str) -> dict:
        return self._post(f"/repos/{repo}/issues/{issue_number}/comments",
                          {"body": body})

    def close_issue(self, repo: str, issue_number: int) -> dict:
        return self._patch(f"/repos/{repo}/issues/{issue_number}",
                           {"state": "closed"})
