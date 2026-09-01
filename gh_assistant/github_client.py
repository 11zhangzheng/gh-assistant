"""Host-side GitHub REST client with bounded retry and PR idempotency."""

from __future__ import annotations

import time
from email.utils import parsedate_to_datetime
from typing import Any


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class GitHubClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.github.com",
        session: Any = None,
    ):
        if not token:
            raise GitHubError("GITHUB_TOKEN is not configured")
        self.base_url = base_url.rstrip("/")
        if session is None:
            try:
                import requests
            except Exception as exc:
                raise GitHubError(
                    f"The requests/SSL runtime is unavailable: {exc}"
                ) from exc
            session = requests.Session()
        self.session = session
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "gh-assistant/0.2",
            }
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                response = self.session.request(
                    method,
                    f"{self.base_url}{path}",
                    params=params,
                    json=json_body,
                    timeout=30,
                )
            except Exception as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(min(2**attempt, 4))
                    continue
                raise GitHubError(f"GitHub network error: {exc}") from exc
            if response.ok:
                return response.json() if response.content else {}
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 3:
                time.sleep(_retry_delay(response, attempt))
                continue
            message = response.text[:500]
            raise GitHubError(
                f"GitHub {method} {path} returned {response.status_code}: {message}",
                response.status_code,
            )
        raise GitHubError(f"GitHub request failed: {last_error}")

    def paginate(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        page = 1
        while len(collected) < limit:
            page_size = min(100, limit - len(collected))
            batch = self.request(
                "GET",
                path,
                params={**(params or {}), "per_page": page_size, "page": page},
            )
            if not isinstance(batch, list):
                raise GitHubError(f"Expected a list from {path}")
            collected.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
        return collected[:limit]

    def get_repo(self, repo: str) -> dict[str, Any]:
        return self.request("GET", f"/repos/{repo}")

    def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        issue = self.request("GET", f"/repos/{repo}/issues/{number}")
        issue["comments_data"] = self.paginate(
            f"/repos/{repo}/issues/{number}/comments", limit=100
        )
        return issue

    def list_issues(self, repo: str, *, state: str = "open", limit: int = 20) -> list[dict[str, Any]]:
        return [
            item
            for item in self.paginate(
                f"/repos/{repo}/issues", params={"state": state}, limit=limit * 2
            )
            if "pull_request" not in item
        ][:limit]

    def list_labels(self, repo: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.paginate(f"/repos/{repo}/labels", limit=limit)

    def add_labels(self, repo: str, issue_number: int, labels: list[str]) -> list[dict[str, Any]]:
        return self.request(
            "POST",
            f"/repos/{repo}/issues/{issue_number}/labels",
            json_body={"labels": labels},
        )

    def comment(self, repo: str, issue_number: int, body: str) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/repos/{repo}/issues/{issue_number}/comments",
            json_body={"body": body},
        )

    def ensure_draft_pr(
        self,
        *,
        repo: str,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
        run_id: str,
    ) -> tuple[dict[str, Any], bool]:
        owner = repo.split("/", 1)[0]
        existing = self.paginate(
            f"/repos/{repo}/pulls",
            params={"state": "open", "head": f"{owner}:{head_branch}"},
            limit=20,
        )
        marker = f"<!-- gh-assistant:{run_id} -->"
        for pull in existing:
            if pull.get("head", {}).get("ref") == head_branch or marker in (pull.get("body") or ""):
                return pull, False
        pull = self.request(
            "POST",
            f"/repos/{repo}/pulls",
            json_body={
                "title": title,
                "head": head_branch,
                "base": base_branch,
                "body": f"{body.rstrip()}\n\n{marker}\n",
                "draft": True,
            },
        )
        return pull, True


def _retry_delay(response: Any, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return min(float(retry_after), 30.0)
        except ValueError:
            try:
                delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
                return max(0.0, min(delay, 30.0))
            except (TypeError, ValueError):
                pass
    return min(0.5 * (2**attempt), 4.0)
