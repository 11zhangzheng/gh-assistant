"""Deterministic fixture benchmark with hidden post-run verification."""

from __future__ import annotations

import html
import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from gh_assistant.config import Settings
from gh_assistant.executors import DockerExecutor, LocalExecutor
from gh_assistant.providers import build_backend
from gh_assistant.report import generate_report
from gh_assistant.state import StateStore
from gh_assistant.workflow import SolveWorkflow


class FixtureGitHub:
    def __init__(self, case: dict[str, Any]):
        self.case = case

    def get_repo(self, repo: str) -> dict[str, Any]:
        return {
            "full_name": repo,
            "default_branch": "main",
            "description": "Deterministic gh-assistant benchmark fixture",
            "private": True,
        }

    def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        del repo
        return {
            "number": number,
            "title": self.case["issue"]["title"],
            "body": self.case["issue"]["body"],
            "state": "open",
            "labels": [{"name": "bug"}],
            "comments_data": [],
        }

    def ensure_draft_pr(self, **kwargs):
        del kwargs
        raise AssertionError("Benchmark workflows must never publish")


def run_benchmark(
    settings: Settings,
    *,
    manifest_path: Path,
    profile: str,
    output_dir: Path,
    limit: int | None = None,
    approval_callback=None,
    backend=None,
) -> dict[str, Any]:
    manifest = _load_manifest(manifest_path)
    cases = manifest["cases"] if limit is None else manifest["cases"][:limit]
    if len(cases) == 0:
        raise ValueError("Benchmark manifest contains no selected cases")
    output_dir.mkdir(parents=True, exist_ok=True)
    workspace_root = output_dir / "workspaces" / profile
    workspace_root.mkdir(parents=True, exist_ok=True)
    state = StateStore(output_dir / "state")
    backend = backend or build_backend(settings)
    docker_ok, _ = DockerExecutor.available()
    docker_image_ok = (
        DockerExecutor(Path.cwd(), image=settings.docker_image).image_available()
        if docker_ok
        else False
    )
    local_approved: bool | None = None
    results = []
    started = time.monotonic()

    def cached_approval(approval: dict[str, Any]) -> bool | None:
        nonlocal local_approved
        if approval["action"] == "local_executor" and local_approved is not None:
            return local_approved
        decision = approval_callback(approval) if approval_callback else None
        if approval["action"] == "local_executor" and decision is not None:
            local_approved = decision
        return decision

    for index, case in enumerate(cases, 1):
        case_started = time.monotonic()
        try:
            case_dir = Path(tempfile.mkdtemp(prefix=f"{case['id']}-", dir=workspace_root))
            repo_path = case_dir / "repo"
            repo_path.mkdir()
            _materialize_case(repo_path, case)
            _init_git(repo_path)
            repo_slug = f"benchmark/{case['id']}"
            workflow = SolveWorkflow(
                settings,
                state=state,
                backend=backend,
                github=FixtureGitHub(case),
                approval_callback=cached_approval,
                profile=profile,
                publish=False,
            )
            run = workflow.start(
                repo=repo_slug,
                issue_number=index,
                repo_path=repo_path,
            )
            hidden = {
                "passed": False,
                "content": "Run did not reach a locally completed patch.",
                "exit_code": None,
            }
            if run["status"] == "completed_local":
                worktree = Path(run["worktree_path"])
                selected = run["result"]["workflow"].get("executor_selected")
                executor = (
                    DockerExecutor(worktree, image=settings.docker_image)
                    if selected == "docker" and docker_image_ok
                    else LocalExecutor(worktree, approved=bool(local_approved))
                )
                execution = executor.run(case["hidden"]["argv"], timeout_seconds=120)
                hidden = {
                    "passed": not execution.is_error,
                    "content": execution.content,
                    "exit_code": (execution.data or {}).get("exit_code"),
                }
            _, case_report = generate_report(state, run["id"])
            results.append(
                {
                    "id": case["id"],
                    "run_id": run["id"],
                    "status": run["status"],
                    "hidden_passed": hidden["passed"],
                    "hidden_exit_code": hidden["exit_code"],
                    "hidden_output": hidden["content"][:2_000],
                    "duration_seconds": round(time.monotonic() - case_started, 3),
                    "report": str(case_report),
                }
            )
        except Exception as exc:
            results.append(
                {
                    "id": case["id"],
                    "run_id": "",
                    "status": "error",
                    "hidden_passed": False,
                    "hidden_exit_code": None,
                    "hidden_output": str(exc),
                    "duration_seconds": round(time.monotonic() - case_started, 3),
                    "report": "",
                }
            )
    passed = sum(1 for item in results if item["hidden_passed"])
    summary = {
        "profile": profile,
        "cases": len(results),
        "hidden_passed": passed,
        "solve_rate": round(passed / len(results), 4),
        "duration_seconds": round(time.monotonic() - started, 3),
    }
    payload = {"summary": summary, "results": results}
    json_path = output_dir / f"results-{profile}.json"
    html_path = output_dir / f"results-{profile}.html"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(_eval_html(payload), encoding="utf-8")
    payload["report_path"] = str(html_path)
    return payload


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        import yaml

        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise ValueError(f"Could not load benchmark manifest {path}: {exc}") from exc
    cases = value.get("cases")
    if not isinstance(cases, list):
        raise ValueError("Benchmark manifest needs a cases array")
    seen = set()
    for case in cases:
        required = {"id", "issue", "files", "verify", "hidden"}
        if not isinstance(case, dict) or not required.issubset(case):
            raise ValueError(f"Invalid benchmark case; required keys: {sorted(required)}")
        if case["id"] in seen:
            raise ValueError(f"Duplicate benchmark case: {case['id']}")
        seen.add(case["id"])
    return value


def _materialize_case(repo_path: Path, case: dict[str, Any]) -> None:
    for relative, content in case["files"].items():
        target = (repo_path / relative).resolve()
        target.relative_to(repo_path.resolve())
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
    try:
        import yaml

        config = {"version": 1, "verify": case["verify"]}
        (repo_path / "gh-assistant.yaml").write_text(
            yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
        )
    except Exception:
        pass


def _init_git(repo_path: Path) -> None:
    commands = [
        ["git", "init", "-b", "main"],
        ["git", "add", "-A"],
        [
            "git",
            "-c",
            "user.name=benchmark",
            "-c",
            "user.email=benchmark@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
    ]
    for command in commands:
        result = subprocess.run(
            command,
            cwd=repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout)


def _eval_html(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    rows = "".join(
        f"<tr><td>{html.escape(item['id'])}</td><td>{html.escape(item['status'])}</td>"
        f"<td>{'PASS' if item['hidden_passed'] else 'FAIL'}</td>"
        f"<td>{item['duration_seconds']}</td><td><code>{html.escape(item['run_id'])}</code></td></tr>"
        for item in payload["results"]
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>gh-assistant benchmark</title>
<style>body{{margin:0;color:#171717;background:#fafafa;font:14px/1.5 system-ui;letter-spacing:0}}header{{background:#fff;border-bottom:1px solid #d7d7d2;padding:24px}}
main{{width:min(1000px,calc(100% - 32px));margin:28px auto}}h1{{margin:0;font-size:24px}}.stats{{display:flex;gap:28px;margin:20px 0}}.stats b{{font-size:22px;display:block;color:#147d4f}}
table{{width:100%;border-collapse:collapse;background:#fff}}th,td{{padding:10px;border-bottom:1px solid #d7d7d2;text-align:left}}th{{background:#f2f2ef;color:#666}}code{{font-family:Consolas,monospace}}</style></head>
<body><header><h1>gh-assistant benchmark · {html.escape(summary['profile'])}</h1></header><main><div class="stats"><span><b>{summary['hidden_passed']}/{summary['cases']}</b>Hidden tests</span>
<span><b>{summary['solve_rate']:.0%}</b>Solve rate</span><span><b>{summary['duration_seconds']}s</b>Duration</span></div>
<table><thead><tr><th>Case</th><th>Run status</th><th>Hidden test</th><th>Seconds</th><th>Run</th></tr></thead><tbody>{rows}</tbody></table></main></body></html>"""
