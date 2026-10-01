"""Deterministic fixture benchmark with hidden post-run verification."""

from __future__ import annotations

import html
import json
import shutil
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
    benchmark_kind = "offline_bug_tasks" if manifest_path.is_dir() else "synthetic"
    all_cases = _load_real_tasks(manifest_path) if manifest_path.is_dir() else _load_manifest(manifest_path)["cases"]
    cases = all_cases if limit is None else all_cases[:limit]
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
            if benchmark_kind == "synthetic":
                repo_path.mkdir()
                _materialize_case(repo_path, case)
                _init_git(repo_path)
            elif case["metadata"]["source"] == "github_issue":
                cloned = subprocess.run(
                    ["git", "clone", "--no-hardlinks", str(case["source_dir"]), str(repo_path)],
                    capture_output=True, text=True, timeout=120, check=False,
                )
                if cloned.returncode:
                    raise RuntimeError(cloned.stderr or cloned.stdout)
                actual = subprocess.run(
                    ["git", "rev-parse", case["base_commit"]], cwd=repo_path,
                    capture_output=True, text=True, timeout=30, check=False,
                )
                if actual.returncode or actual.stdout.strip() != case["base_commit"]:
                    raise ValueError(f"Base commit not present for {case['id']}")
            else:
                shutil.copytree(case["source_dir"], repo_path)
                _init_git(repo_path)
            repo_slug = case.get("repo", f"benchmark/{case['id']}")
            workflow = SolveWorkflow(
                settings,
                state=state,
                backend=backend,
                github=FixtureGitHub(case),
                approval_callback=cached_approval,
                profile=profile,
                publish=False,
            )
            start_args = {"repo": repo_slug, "issue_number": case.get("issue_number", index), "repo_path": repo_path}
            if case.get("base_commit"):
                start_args["base_ref"] = case["base_commit"]
            run = workflow.start(**start_args)
            hidden = {
                "passed": False if case.get("hidden") else None,
                "content": "Run did not reach a locally completed patch." if case.get("hidden") else "No hidden judge configured.",
                "exit_code": None,
            }
            if run["status"] == "completed_local" and case.get("hidden"):
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
            case_json, case_report = generate_report(state, run["id"])
            report_data = json.loads(case_json.read_text(encoding="utf-8"))
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
                    "outcome": run["result"].get("outcome"),
                    "evidence": report_data["result"].get("evidence", {}),
                    "usage": report_data["usage"],
                    "agent_iterations": sum(
                        event["event_type"] == "model_completed" and event["actor"] == "main"
                        for event in state.events(run["id"])
                    ),
                    "human": case.get("human", {}),
                    "benchmark_kind": benchmark_kind,
                    "source": case.get("metadata", {}).get("source", "synthetic"),
                }
            )
        except Exception as exc:
            results.append(
                {
                    "id": case["id"],
                    "run_id": "",
                    "status": "error",
                    "hidden_passed": False if case.get("hidden") else None,
                    "hidden_exit_code": None,
                    "hidden_output": str(exc),
                    "duration_seconds": round(time.monotonic() - case_started, 3),
                    "report": "",
                    "outcome": None,
                    "evidence": {}, "usage": {}, "human": case.get("human", {}),
                    "benchmark_kind": benchmark_kind,
                }
            )
    summary = summarize_results(results, profile)
    summary["benchmark_kind"] = benchmark_kind
    summary["duration_seconds"] = round(time.monotonic() - started, 3)
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


def _load_real_tasks(root: Path) -> list[dict[str, Any]]:
    """Load offline task snapshots; provenance determines whether a task is a real Issue."""
    import re
    import yaml

    tasks = []
    seen = set()
    for path in sorted(root.glob("*/task.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        task_dir = path.parent.resolve()
        required = {"id", "repo", "issue_number", "language", "issue_type", "reproduction", "verification", "metadata"}
        if not isinstance(raw, dict) or not required.issubset(raw):
            raise ValueError(f"Invalid real task {path}; required keys: {sorted(required)}")
        if raw["id"] != task_dir.name or raw["id"] in seen:
            raise ValueError(f"Task id mismatch or duplicate: {raw['id']}")
        seen.add(raw["id"])
        if raw["language"] != "python" or raw["issue_type"] != "bug":
            raise ValueError(f"Only Python bug tasks are supported: {raw['id']}")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", str(raw["repo"])):
            raise ValueError(f"Invalid repository slug: {raw['repo']}")
        source = raw["metadata"].get("source")
        source_dir = task_dir / "repository"
        if source not in {"github_issue", "high_realism_fixture"} or not source_dir.is_dir() or source_dir.is_symlink():
            raise ValueError(f"Task source or repository snapshot is invalid: {raw['id']}")
        if not (task_dir / "issue.md").is_file():
            raise ValueError(f"Task issue.md is missing: {raw['id']}")
        if source == "github_issue":
            if not re.fullmatch(r"https://github\.com/[^/]+/[^/]+/issues/\d+", str(raw.get("issue_url") or "")):
                raise ValueError(f"Real GitHub task requires issue_url: {raw['id']}")
            if not re.fullmatch(r"[0-9a-f]{40}", str(raw.get("base_commit") or "")) or not (source_dir / ".git").exists():
                raise ValueError(f"Real GitHub task requires a local git snapshot and base_commit: {raw['id']}")
        else:
            if raw.get("issue_url") or raw.get("base_commit") or (source_dir / ".git").exists():
                raise ValueError(f"High-realism fixture must not claim a real issue or base commit: {raw['id']}")
        reproduction = raw["reproduction"]
        verification = raw["verification"]
        if not isinstance(reproduction, dict) or not isinstance(verification, dict):
            raise ValueError(f"Invalid command sections: {raw['id']}")
        for command in [reproduction.get("command"), verification.get("targeted"), verification.get("hidden")]:
            if command is not None and (not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg for arg in command)):
                raise ValueError(f"Invalid argv command: {raw['id']}")
        human = raw.get("human") or {}
        if not isinstance(human, dict):
            raise ValueError(f"Human annotation must be an object: {raw['id']}")
        tasks.append({
            "id": raw["id"], "repo": raw["repo"], "issue_number": raw["issue_number"],
            "issue": {"title": raw.get("issue_title") or raw["id"], "body": (task_dir / "issue.md").read_text(encoding="utf-8")},
            "source_dir": source_dir, "base_commit": raw.get("base_commit"),
            "hidden": {"argv": verification["hidden"]} if verification.get("hidden") else None,
            "reproduction": reproduction, "verification": verification,
            "metadata": raw["metadata"], "human": human,
        })
    if not tasks:
        raise ValueError(f"No real-issue tasks found under {root}")
    return tasks


def summarize_results(results: list[dict[str, Any]], profile: str) -> dict[str, Any]:
    """Aggregate only measured values; unavailable human/cost data stays null."""
    if not results:
        raise ValueError("Cannot summarize an empty benchmark")
    count = len(results)

    def rate(predicate) -> float:
        return round(sum(bool(predicate(item)) for item in results) / count, 4)

    judged = [item for item in results if item.get("hidden_passed") is not None]
    annotated = [item for item in results if item.get("human", {}).get("incorrect_fix") is not None]
    timed = [
        item for item in results
        if isinstance(item.get("human", {}).get("accepted"), bool)
        and item["human"].get("maintainer_review_minutes") is not None
        and item["human"].get("manual_debug_minutes") is not None
    ]
    accepted_count = sum(item["human"]["accepted"] is True for item in timed)
    usage = [item.get("usage") or {} for item in results]
    known_cost = [entry["estimated_cost_usd"] for entry in usage if entry.get("estimated_cost_usd") is not None]
    return {
        "profile": profile, "cases": count,
        "hidden_passed": sum(item.get("hidden_passed") is True for item in judged),
        "hidden_judged": len(judged),
        "solve_rate": round(sum(item["hidden_passed"] is True for item in judged) / len(judged), 4) if judged else None,
        "verified_fix_rate": rate(lambda item: item.get("outcome") == "VERIFIED_FIX"),
        "candidate_fix_rate": rate(lambda item: item.get("outcome") == "CANDIDATE_FIX"),
        "abstain_rate": rate(lambda item: item.get("outcome") == "ABSTAIN"),
        "incorrect_fix_rate": round(sum(item["human"]["incorrect_fix"] is True for item in annotated) / len(annotated), 4) if annotated else None,
        "incorrect_fix_annotated": len(annotated),
        "reproduction_success_rate": rate(lambda item: item.get("evidence", {}).get("reproduction", {}).get("status") == "PASS"),
        "regression_evidence_rate": rate(lambda item: item.get("evidence", {}).get("regression", {}).get("before", {}).get("status") == "FAIL" and item.get("evidence", {}).get("regression", {}).get("after", {}).get("status") == "PASS"),
        "repository_verification_rate": rate(lambda item: bool(item.get("evidence", {}).get("repository_checks")) and all(check.get("status") == "PASS" for check in item["evidence"]["repository_checks"])),
        "scope_violation_rate": rate(lambda item: item.get("evidence", {}).get("fix_scope", {}).get("violation") is True),
        "review_blocker_rate": rate(lambda item: bool(item.get("evidence", {}).get("review", {}).get("blockers"))),
        "wall_clock_seconds": round(sum(float(item.get("duration_seconds") or 0) for item in results), 3),
        "model_calls": sum(int(entry.get("model_calls") or 0) for entry in usage),
        "agent_iterations": sum(int(item.get("agent_iterations") or 0) for item in results),
        "input_tokens": sum(int(entry.get("input_tokens") or 0) for entry in usage),
        "output_tokens": sum(int(entry.get("output_tokens") or 0) for entry in usage),
        "estimated_model_cost_usd": round(sum(known_cost), 6) if known_cost else None,
        "tool_calls": sum(int(entry.get("tool_calls") or 0) for entry in usage),
        "maintainer_intervention_minutes_per_accepted_fix": round(sum(
            item["human"]["maintainer_review_minutes"] + item["human"]["manual_debug_minutes"]
            for item in timed
        ) / accepted_count, 3) if len(timed) == count and accepted_count else None,
        "human_time_annotated": len(timed),
        "accepted_with_complete_time_annotation": accepted_count,
        "maintainer_interventions": sum(item["human"]["maintainer_interventions"] for item in results)
        if all(item.get("human", {}).get("maintainer_interventions") is not None for item in results) else None,
        "requested_revisions": sum(item["human"]["requested_revisions"] for item in results)
        if all(item.get("human", {}).get("requested_revisions") is not None for item in results) else None,
    }


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
    solve_label = f"{summary['solve_rate']:.0%}" if summary["solve_rate"] is not None else "n/a"
    rows = "".join(
        f"<tr><td>{html.escape(item['id'])}</td><td>{html.escape(item['status'])}</td>"
        f"<td>{html.escape(str(item.get('outcome') or 'UNCLASSIFIED'))}</td>"
        f"<td>{'NOT_JUDGED' if item['hidden_passed'] is None else ('PASS' if item['hidden_passed'] else 'FAIL')}</td>"
        f"<td>{item['duration_seconds']}</td><td><code>{html.escape(item['run_id'])}</code></td></tr>"
        for item in payload["results"]
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>gh-assistant benchmark</title>
<style>body{{margin:0;color:#171717;background:#fafafa;font:14px/1.5 system-ui;letter-spacing:0}}header{{background:#fff;border-bottom:1px solid #d7d7d2;padding:24px}}
main{{width:min(1000px,calc(100% - 32px));margin:28px auto}}h1{{margin:0;font-size:24px}}.stats{{display:flex;gap:28px;margin:20px 0}}.stats b{{font-size:22px;display:block;color:#147d4f}}
table{{width:100%;border-collapse:collapse;background:#fff}}th,td{{padding:10px;border-bottom:1px solid #d7d7d2;text-align:left}}th{{background:#f2f2ef;color:#666}}code{{font-family:Consolas,monospace}}</style></head>
<body><header><h1>gh-assistant benchmark · {html.escape(summary['profile'])}</h1></header><main><div class="stats"><span><b>{summary['hidden_passed']}/{summary['cases']}</b>Hidden tests</span>
  <span><b>{solve_label}</b>Solve rate</span><span><b>{summary['duration_seconds']}s</b>Duration</span></div>
  <p>Verified: {summary['verified_fix_rate']:.0%} · Candidate: {summary['candidate_fix_rate']:.0%} · Abstain: {summary['abstain_rate']:.0%}</p>
  <table><thead><tr><th>Case</th><th>Run status</th><th>Outcome</th><th>Hidden test</th><th>Seconds</th><th>Run</th></tr></thead><tbody>{rows}</tbody></table></main></body></html>"""
