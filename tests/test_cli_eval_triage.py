from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gh_assistant import cli, evaluation
from gh_assistant.config import Settings
from gh_assistant.contracts import ModelResponse, TextPart, ToolEffect, ToolExecution
from gh_assistant.providers import ScriptedBackend
from gh_assistant.state import StateStore
from gh_assistant.tools import object_schema
from gh_assistant.triage import TriageWorkflow, _issue_summary


class TriageGitHub:
    def __init__(self):
        self.added = []
        self.comments = []

    def get_repo(self, repo):
        return {"full_name": repo, "description": "repo", "default_branch": "main"}

    def list_issues(self, repo, limit=10):
        return [{"number": 1, "title": "Bug", "labels": [{"name": "bug"}], "state": "open"}]

    def list_labels(self, repo):
        return [{"name": "bug"}, {"name": "question"}]

    def get_issue(self, repo, number):
        return {
            "number": number,
            "title": "Bug",
            "body": "details",
            "state": "open",
            "labels": [],
            "comments_data": [{"user": {"login": "dev"}, "body": "seen"}],
        }

    def add_labels(self, repo, number, labels):
        self.added.append((repo, number, labels))
        return [{"name": value} for value in labels]

    def comment(self, repo, number, body):
        self.comments.append((repo, number, body))
        return {"html_url": "https://example.invalid/comment"}


def test_triage_workflow_and_registry(tmp_path: Path):
    settings = Settings(provider="scripted", model="test", state_dir=tmp_path / "state")
    state = StateStore(settings.state_dir)
    github = TriageGitHub()
    workflow = TriageWorkflow(
        settings,
        state=state,
        backend=ScriptedBackend([ModelResponse([TextPart("triaged")])]),
        github=github,
    )
    run = workflow.start("owner/repo", limit=3)
    assert run["status"] == "succeeded"
    assert run["result"]["summary"] == "triaged"

    registry = workflow._registry(run)
    assert "owner/repo" in registry.execute("repo_info", {}).content
    assert "Bug" in registry.execute("list_issues", {}).content
    assert "details" in registry.execute("get_issue", {"number": 1}).content
    assert "question" in registry.execute("list_labels", {}).content
    assert registry.execute("add_labels", {"issue_number": 1, "labels": ["missing"]}).is_error
    assert not registry.execute("add_labels", {"issue_number": 1, "labels": ["bug"]}).is_error
    assert not registry.execute("comment_on_issue", {"issue_number": 1, "body": "hello"}).is_error
    assert github.added and github.comments

    resumed = TriageWorkflow(
        settings,
        state=state,
        backend=ScriptedBackend([ModelResponse([TextPart("resumed")])]),
        github=github,
    ).resume(run["id"])
    assert resumed["result"]["summary"] == "resumed"


def test_triage_helpers_bound_untrusted_content():
    issue = {
        "number": 2,
        "title": "x",
        "state": "open",
        "labels": [{"name": "bug"}],
        "body": "a" * 60_000,
        "comments_data": [
            {"user": {}, "body": "b" * 20_000} for _ in range(25)
        ],
    }
    value = _issue_summary(issue, full=True)
    assert len(value["body"]) == 50_000
    assert len(value["comments"]) == 20
    assert value["comments"][0]["author"] == "unknown"
    assert object_schema({})["additionalProperties"] is False


def test_triage_dry_run_persists_and_never_writes(tmp_path: Path):
    settings = Settings(provider="scripted", model="test", state_dir=tmp_path / "state")
    github = TriageGitHub()
    workflow = TriageWorkflow(
        settings,
        state=StateStore(settings.state_dir),
        backend=ScriptedBackend([ModelResponse([TextPart("previewed")])]),
        github=github,
        dry_run=True,
    )
    run = workflow.start("owner/repo")
    registry = workflow._registry(run)

    labels = registry.execute("add_labels", {"issue_number": 1, "labels": ["bug"]})
    comment = registry.execute("comment_on_issue", {"issue_number": 1, "body": "hello"})

    assert run["config"]["dry_run"] is True
    assert labels.data == {"labels": ["bug"], "dry_run": True}
    assert comment.data == {"body": "hello", "dry_run": True}
    assert registry.spec("add_labels").effect == ToolEffect.READ
    assert registry.spec("comment_on_issue").effect == ToolEffect.READ
    assert github.added == []
    assert github.comments == []


def _manifest(path: Path) -> Path:
    path.write_text(
        "cases:\n"
        "  - id: one\n"
        "    issue: {title: Fix, body: Fix it}\n"
        "    files: {bug.py: 'value = 1'}\n"
        "    verify: [['python', '-c', 'pass']]\n"
        "    hidden: {argv: ['python', '-c', 'pass']}\n",
        encoding="utf-8",
    )
    return path


def test_benchmark_loader_materialization_and_run(tmp_path: Path, monkeypatch):
    manifest = _manifest(tmp_path / "manifest.yaml")
    assert evaluation._load_manifest(manifest)["cases"][0]["id"] == "one"
    repo = tmp_path / "materialized"
    repo.mkdir()
    evaluation._materialize_case(repo, evaluation._load_manifest(manifest)["cases"][0])
    assert (repo / "bug.py").read_text(encoding="utf-8") == "value = 1"
    assert (repo / "gh-assistant.yaml").exists()

    class FakeWorkflow:
        def __init__(self, settings, **kwargs):
            self.state = kwargs["state"]
            self.approval_callback = kwargs["approval_callback"]

        def start(self, repo, issue_number, repo_path):
            self.approval_callback({"action": "local_executor"})
            run = self.state.create_run(
                repo=repo,
                issue_number=issue_number,
                repo_path=repo_path,
                provider="scripted",
                model="fake",
                config={},
            )
            result = {"workflow": {"executor_selected": "local"}}
            return self.state.update_run(
                run["id"],
                worktree_path=str(repo_path),
                status="completed_local",
                phase="done",
                result_json=result,
            )

    monkeypatch.setattr(evaluation, "SolveWorkflow", FakeWorkflow)
    monkeypatch.setattr(evaluation.DockerExecutor, "available", classmethod(lambda cls: (False, "off")))
    settings = Settings(provider="scripted", model="fake", executor="local")
    result = evaluation.run_benchmark(
        settings,
        manifest_path=manifest,
        profile="baseline",
        output_dir=tmp_path / "eval",
        approval_callback=lambda approval: True,
        backend=object(),
    )
    assert result["summary"]["solve_rate"] == 1.0
    assert Path(result["report_path"]).exists()
    assert json.loads((tmp_path / "eval" / "results-baseline.json").read_text())["results"][0]["hidden_passed"]


@pytest.mark.parametrize(
    "content,match",
    [
        ("x: 1\n", "cases array"),
        ("cases: [{}]\n", "required keys"),
        (
            "cases:\n  - &c {id: x, issue: {}, files: {}, verify: [], hidden: {}}\n  - *c\n",
            "Duplicate",
        ),
    ],
)
def test_benchmark_manifest_rejects_invalid_input(tmp_path: Path, content: str, match: str):
    path = tmp_path / "bad.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        evaluation._load_manifest(path)


def test_benchmark_records_case_errors_and_empty_selection(tmp_path: Path, monkeypatch):
    manifest = _manifest(tmp_path / "manifest.yaml")
    monkeypatch.setattr(evaluation, "_init_git", lambda path: (_ for _ in ()).throw(RuntimeError("git failed")))
    result = evaluation.run_benchmark(
        Settings(provider="scripted", model="fake"),
        manifest_path=manifest,
        profile="full",
        output_dir=tmp_path / "eval",
        backend=object(),
    )
    assert result["results"][0]["status"] == "error"
    assert "git failed" in result["results"][0]["hidden_output"]
    with pytest.raises(ValueError, match="no selected cases"):
        evaluation.run_benchmark(
            Settings(provider="scripted", model="fake"),
            manifest_path=manifest,
            profile="full",
            output_dir=tmp_path / "empty",
            limit=0,
            backend=object(),
        )


class FakeCliState:
    runs = []
    approvals = []

    def __init__(self, path):
        self.path = path

    def get_run(self, run_id):
        return next(item for item in self.runs if item["id"] == run_id)

    def list_runs(self, limit=50):
        return self.runs[:limit]

    def list_approvals(self, run_id=None, status=None):
        return [
            item for item in self.approvals
            if (run_id is None or item["run_id"] == run_id)
            and (status is None or item["status"] == status)
        ]

    def decide_approval(self, approval_id, decision):
        item = next(value for value in self.approvals if value["id"] == approval_id)
        item["status"] = decision
        return item


def _args(command: str, **kwargs):
    base = {"command": command}
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_cli_dispatches_listing_approvals_reports_and_eval(tmp_path: Path, monkeypatch, capsys):
    run = {
        "id": "run1", "status": "completed_local", "phase": "done",
        "repo": "o/r", "issue_number": 1, "result": {}, "config": {},
    }
    approval = {"id": "a1", "status": "pending", "action": "publish", "run_id": "run1"}
    FakeCliState.runs = [run]
    FakeCliState.approvals = [approval]
    monkeypatch.setattr(cli, "StateStore", FakeCliState)
    monkeypatch.setattr(cli, "generate_report", lambda *a, **k: (tmp_path / "r.json", tmp_path / "r.html"))
    settings = Settings(provider="scripted", model="fake", state_dir=tmp_path)
    assert cli._dispatch(_args("runs", limit=10), settings) == 0
    assert cli._dispatch(_args("approvals", approval_command=None, run=None, status=None), settings) == 0
    assert cli._dispatch(_args("approvals", approval_command="approve", approval_id="a1"), settings) == 0
    assert cli._dispatch(_args("report", run_id="run1", output=None, include_content=False), settings) == 0
    assert "run1" in capsys.readouterr().out

    import gh_assistant.evaluation as eval_module
    monkeypatch.setattr(
        eval_module,
        "run_benchmark",
        lambda *a, **k: {"summary": {"solve_rate": 1}, "report_path": "eval.html"},
    )
    assert cli._dispatch(
        _args("eval", manifest="m.yaml", profile="full", output="out", limit=None), settings
    ) == 0
    with pytest.raises(ValueError, match="Unknown command"):
        cli._dispatch(_args("unknown"), settings)


def test_cli_dispatches_solve_triage_and_resume(tmp_path: Path, monkeypatch):
    solve_run = {
        "id": "solve", "status": "completed_local", "phase": "done",
        "repo": "o/r", "issue_number": 1, "result": {},
        "config": {"kind": "solve", "executor_preference": "local", "profile": "baseline", "publish": False},
    }
    triage_run = {
        "id": "triage", "status": "succeeded", "phase": "done",
        "repo": "o/r", "issue_number": 0, "result": {},
        "config": {"kind": "triage", "dry_run": True},
    }
    FakeCliState.runs = [solve_run, triage_run]
    FakeCliState.approvals = []
    monkeypatch.setattr(cli, "StateStore", FakeCliState)
    monkeypatch.setattr(cli, "generate_report", lambda *a, **k: (tmp_path / "r.json", tmp_path / "r.html"))

    class FakeSolve:
        def __init__(self, settings, **kwargs): pass
        def start(self, **kwargs): return solve_run
        def resume(self, run_id): return solve_run

    class FakeTriage:
        dry_runs = []

        def __init__(self, settings, **kwargs):
            self.dry_runs.append(kwargs["dry_run"])

        def start(self, repo, limit):
            return triage_run

        def resume(self, run_id):
            return triage_run

    monkeypatch.setattr(cli, "SolveWorkflow", FakeSolve)
    monkeypatch.setattr(cli, "TriageWorkflow", FakeTriage)
    settings = Settings(provider="scripted", model="fake", state_dir=tmp_path)
    solve_args = _args(
        "solve",
        repo="o/r",
        issue=1,
        path=str(tmp_path),
        base="",
        profile="full",
        no_publish=True,
    )
    assert cli._dispatch(solve_args, settings) == 0
    assert cli._dispatch(_args("triage", repo="o/r", limit=2, dry_run=False), settings) == 0
    assert cli._dispatch(_args("resume", run_id="solve"), settings) == 0
    assert cli._dispatch(_args("resume", run_id="triage"), settings) == 0
    assert FakeTriage.dry_runs == [False, True]

    parsed = cli._parser().parse_args(["triage", "o/r", "--dry-run"])
    assert parsed.dry_run is True


def test_cli_main_error_interrupt_and_approval(monkeypatch, capsys):
    monkeypatch.setattr(cli.Settings, "from_env", classmethod(lambda cls, state_dir=None: Settings(provider="scripted", model="fake")))
    monkeypatch.setattr(cli, "_dispatch", lambda args, settings: 7)
    assert cli.main(["runs"]) == 7
    monkeypatch.setattr(cli, "_dispatch", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert cli.main(["runs"]) == 130
    monkeypatch.setattr(cli, "_dispatch", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cli.main(["runs"]) == 1
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    assert cli._approval_prompt({"id": "a", "action": "publish", "payload": {}}) is True
    assert "boom" in capsys.readouterr().err
