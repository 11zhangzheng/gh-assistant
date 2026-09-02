"""Command-line interface for gh-assistant."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
from pathlib import Path

from gh_assistant.config import Settings
from gh_assistant.executors import DockerExecutor
from gh_assistant.github_client import GitHubClient
from gh_assistant.report import generate_report
from gh_assistant.state import StateStore
from gh_assistant.triage import TriageWorkflow
from gh_assistant.workflow import SolveWorkflow


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    settings = Settings.from_env(state_dir=args.state_dir)
    if getattr(args, "provider", None):
        settings.provider = args.provider
    if getattr(args, "model", None):
        settings.model = args.model
    if getattr(args, "executor", None):
        settings.executor = args.executor
    if getattr(args, "non_interactive", False):
        settings.interactive = False
    try:
        return _dispatch(args, settings)
    except KeyboardInterrupt:
        print("\nInterrupted; checkpoint preserved.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _dispatch(args, settings: Settings) -> int:
    state = StateStore(settings.state_dir)
    if args.command == "doctor":
        return _doctor(settings, args.repo)
    if args.command == "solve":
        workflow = SolveWorkflow(
            settings,
            state=state,
            approval_callback=_approval_prompt if settings.interactive else None,
            profile=args.profile,
            publish=not args.no_publish,
        )
        run = workflow.start(
            repo=args.repo,
            issue_number=args.issue,
            repo_path=Path(args.path),
            base_ref=args.base,
        )
        return _print_run(state, run)
    if args.command == "triage":
        workflow = TriageWorkflow(
            settings,
            state=state,
            approval_callback=_approval_prompt if settings.interactive else None,
            dry_run=args.dry_run,
        )
        run = workflow.start(args.repo, limit=args.limit)
        return _print_run(state, run)
    if args.command == "resume":
        run = state.get_run(args.run_id)
        if run["config"].get("kind") == "triage":
            workflow = TriageWorkflow(
                settings,
                state=state,
                approval_callback=_approval_prompt if settings.interactive else None,
                dry_run=bool(run["config"].get("dry_run", False)),
            )
        else:
            settings.executor = run["config"].get("executor_preference", settings.executor)
            settings.docker_image = run["config"].get("docker_image", settings.docker_image)
            workflow = SolveWorkflow(
                settings,
                state=state,
                approval_callback=_approval_prompt if settings.interactive else None,
                profile=run["config"].get("profile", "full"),
                publish=bool(run["config"].get("publish", True)),
            )
        return _print_run(state, workflow.resume(args.run_id))
    if args.command == "runs":
        for run in state.list_runs(limit=args.limit):
            print(
                f"{run['id']}  {run['status']:<18} {run['phase']:<15} "
                f"{run['repo']}#{run['issue_number']}"
            )
        return 0
    if args.command == "approvals":
        if args.approval_command in {"approve", "deny"}:
            decision = "approved" if args.approval_command == "approve" else "denied"
            approval = state.decide_approval(args.approval_id, decision)
            print(f"{approval['id']}: {approval['status']}")
            return 0
        approvals = state.list_approvals(run_id=args.run, status=args.status)
        for item in approvals:
            print(
                f"{item['id']}  {item['status']:<9} {item['action']:<24} {item['run_id']}"
            )
        return 0
    if args.command == "report":
        json_path, html_path = generate_report(
            state,
            args.run_id,
            output_dir=Path(args.output) if args.output else None,
            include_content=args.include_content,
        )
        print(f"JSON: {json_path}")
        print(f"HTML: {html_path}")
        return 0
    if args.command == "eval":
        from gh_assistant.evaluation import run_benchmark

        result = run_benchmark(
            settings,
            manifest_path=Path(args.manifest),
            profile=args.profile,
            output_dir=Path(args.output),
            limit=args.limit,
            approval_callback=_approval_prompt if settings.interactive else None,
        )
        print(json.dumps(result["summary"], indent=2))
        print(f"Report: {result['report_path']}")
        return 0
    raise ValueError(f"Unknown command: {args.command}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gha", description="Auditable GitHub software-engineering agent"
    )
    parser.add_argument("--state-dir", default=".gha", help="runtime state directory")
    parser.add_argument("--provider", choices=["anthropic", "openai"])
    parser.add_argument("--model")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check local and external prerequisites")
    doctor.add_argument("--repo", help="optionally check read access to owner/name")

    solve = sub.add_parser("solve", help="solve one GitHub issue in an isolated worktree")
    solve.add_argument("repo", help="owner/name")
    solve.add_argument("issue", type=int)
    solve.add_argument("--path", required=True, help="local checkout of the target repo")
    solve.add_argument("--base", default="", help="local base ref; defaults to origin/default or HEAD")
    solve.add_argument("--executor", choices=["auto", "docker", "local"])
    solve.add_argument("--profile", choices=["baseline", "full"], default="full")
    solve.add_argument("--no-publish", action="store_true", help="stop with a local patch")
    solve.add_argument("--non-interactive", action="store_true")

    triage = sub.add_parser("triage", help="triage open GitHub issues")
    triage.add_argument("repo")
    triage.add_argument("--limit", type=int, default=10)
    triage.add_argument(
        "--dry-run", action="store_true", help="simulate labels and comments"
    )
    triage.add_argument("--non-interactive", action="store_true")

    resume = sub.add_parser("resume", help="resume a checkpointed run")
    resume.add_argument("run_id")
    resume.add_argument("--non-interactive", action="store_true")

    runs = sub.add_parser("runs", help="list recent runs")
    runs.add_argument("--limit", type=int, default=50)

    approvals = sub.add_parser("approvals", help="list or decide pending approvals")
    approval_sub = approvals.add_subparsers(dest="approval_command")
    approvals.add_argument("--run")
    approvals.add_argument("--status", choices=["pending", "approved", "denied"])
    approve = approval_sub.add_parser("approve")
    approve.add_argument("approval_id")
    deny = approval_sub.add_parser("deny")
    deny.add_argument("approval_id")

    report = sub.add_parser("report", help="generate sanitized JSON and HTML reports")
    report.add_argument("run_id")
    report.add_argument("--output")
    report.add_argument("--include-content", action="store_true")

    evaluate = sub.add_parser("eval", help="run deterministic benchmark fixtures")
    evaluate.add_argument("manifest")
    evaluate.add_argument("--profile", choices=["baseline", "full"], default="full")
    evaluate.add_argument("--output", default=".gha/eval")
    evaluate.add_argument("--limit", type=int)
    evaluate.add_argument("--executor", choices=["auto", "docker", "local"])
    evaluate.add_argument("--non-interactive", action="store_true")
    return parser


def _doctor(settings: Settings, repo: str | None) -> int:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("Python", sys.version_info >= (3, 11), sys.version.split()[0]))
    checks.append(("Git", bool(shutil.which("git")), shutil.which("git") or "not found"))
    docker_ok, docker_detail = DockerExecutor.available()
    checks.append(("Docker daemon", docker_ok, docker_detail))
    image_ok = False
    if docker_ok:
        image_ok = DockerExecutor(Path.cwd(), image=settings.docker_image).image_available()
    checks.append(("Docker image", image_ok, settings.docker_image))
    package = "anthropic" if settings.provider == "anthropic" else "openai"
    checks.append((f"{package} SDK", importlib.util.find_spec(package) is not None, package))
    provider_errors = settings.validate_provider()
    checks.append(("Model config", not provider_errors, "; ".join(provider_errors) or settings.model))
    try:
        StateStore(settings.state_dir)
        checks.append(("State store", True, str(Path(settings.state_dir).resolve())))
    except Exception as exc:
        checks.append(("State store", False, str(exc)))
    if repo:
        try:
            info = GitHubClient(settings.github_token).get_repo(repo)
            checks.append(("GitHub read", True, info.get("full_name", repo)))
        except Exception as exc:
            checks.append(("GitHub read", False, str(exc)))
    width = max(len(name) for name, _, _ in checks)
    for name, ok, detail in checks:
        print(f"{'OK' if ok else 'FAIL':<4} {name:<{width}}  {detail}")
    required = [item for item in checks if item[0] not in {"Docker daemon", "Docker image"}]
    return 0 if all(ok for _, ok, _ in required) else 1


def _approval_prompt(approval: dict) -> bool | None:
    print("\nApproval required")
    print(f"  ID:     {approval['id']}")
    print(f"  Action: {approval['action']}")
    print(json.dumps(approval.get("payload", {}), ensure_ascii=False, indent=2)[:4_000])
    answer = input("Approve this exact action? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def _print_run(state: StateStore, run: dict) -> int:
    print(f"Run:    {run['id']}")
    print(f"Status: {run['status']}")
    print(f"Phase:  {run['phase']}")
    if run["result"].get("needs_human_reason"):
        print(f"Reason: {run['result']['needs_human_reason']}")
    pending = state.list_approvals(run_id=run["id"], status="pending")
    for approval in pending:
        print(f"Pending approval: {approval['id']} ({approval['action']})")
    _, html_path = generate_report(state, run["id"])
    print(f"Report: {html_path}")
    return 0 if run["status"] not in {"failed", "needs_human"} else 1
