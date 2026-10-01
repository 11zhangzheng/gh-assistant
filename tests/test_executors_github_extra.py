from __future__ import annotations

import subprocess
import os
import py_compile
from pathlib import Path
from types import SimpleNamespace

import pytest

from gh_assistant import executors
from gh_assistant.executors import (
    DockerExecutor,
    ExecutorUnavailable,
    LocalExecutor,
    _cap,
    _prepare_command,
    choose_executor,
)
from gh_assistant.contracts import ToolExecution
from gh_assistant.github_client import GitHubClient, GitHubError, _retry_delay


class Response:
    def __init__(self, status=200, payload=None, text="", headers=None, content=b"x"):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload
        self.text = text
        self.headers = headers or {}
        self.content = content

    def json(self):
        return self._payload


class Session:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []
        self.headers = {}

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def test_github_request_retry_pagination_and_helpers(monkeypatch):
    monkeypatch.setattr("gh_assistant.github_client.time.sleep", lambda seconds: None)
    session = Session([
        RuntimeError("net"),
        Response(503, text="retry", headers={"Retry-After": "0"}),
        Response(200, {"full_name": "o/r"}),
    ])
    client = GitHubClient("token-value", base_url="https://api.invalid/", session=session)
    assert client.get_repo("o/r")["full_name"] == "o/r"
    assert session.headers["Authorization"].startswith("Bearer")

    pages = Session([
        Response(200, [{"id": value} for value in range(100)]),
        Response(200, [{"id": 100}]),
    ])
    paged = GitHubClient("token", session=pages)
    assert len(paged.paginate("/items", limit=101)) == 101
    bad = GitHubClient("token", session=Session([Response(200, {"not": "list"})]))
    with pytest.raises(GitHubError, match="Expected a list"):
        bad.paginate("/bad")

    api = GitHubClient("token", session=Session([
        Response(200, {"number": 1}), Response(200, []),
        Response(200, [{"number": 1}]),
        Response(200, [{"name": "bug"}]),
        Response(200, [{"name": "bug"}]),
        Response(200, {"html_url": "comment"}),
    ]))
    assert api.get_issue("o/r", 1)["comments_data"] == []
    assert len(api.list_issues("o/r", limit=1)) == 1
    assert api.list_labels("o/r")[0]["name"] == "bug"
    assert api.add_labels("o/r", 1, ["bug"])[0]["name"] == "bug"
    assert api.comment("o/r", 1, "body")["html_url"] == "comment"


def test_github_errors_pr_idempotency_and_retry_delay(monkeypatch):
    with pytest.raises(GitHubError, match="not configured"):
        GitHubClient("")
    monkeypatch.setattr("gh_assistant.github_client.time.sleep", lambda seconds: None)
    failing = GitHubClient("token", session=Session([Response(404, text="missing")]))
    with pytest.raises(GitHubError) as error:
        failing.request("GET", "/missing")
    assert error.value.status == 404
    network = GitHubClient("token", session=Session([RuntimeError("x")] * 4))
    with pytest.raises(GitHubError, match="network error"):
        network.request("GET", "/x")

    existing = {"number": 3, "head": {"ref": "gha/run"}, "body": ""}
    client = GitHubClient("token", session=Session([Response(200, [existing])]))
    value, created = client.ensure_draft_pr(
        repo="owner/repo", head_branch="gha/run", base_branch="main",
        title="Fix", body="body", run_id="r1",
    )
    assert value["number"] == 3 and not created

    session = Session([Response(200, []), Response(200, {"number": 4, "draft": True})])
    client = GitHubClient("token", session=session)
    value, created = client.ensure_draft_pr(
        repo="owner/repo", head_branch="gha/new", base_branch="main",
        title="Fix", body="body ", run_id="r2",
    )
    assert created and value["number"] == 4
    assert "<!-- gh-assistant:r2 -->" in session.calls[-1][2]["json"]["body"]
    assert _retry_delay(Response(headers={"Retry-After": "99"}), 0) == 30.0
    assert _retry_delay(Response(headers={"Retry-After": "bad"}), 2) == 2.0


def test_local_executor_validation_success_failure_and_output_cap(tmp_path: Path):
    denied = LocalExecutor(tmp_path, approved=False).run(["python", "-c", "print('x')"])
    assert denied.is_error
    ok = LocalExecutor(tmp_path, approved=True).run(["python", "-c", "print('ok')"])
    assert not ok.is_error and "ok" in ok.content
    failed = LocalExecutor(tmp_path, approved=True).run(["python", "-c", "raise SystemExit(3)"])
    assert failed.is_error and failed.data["exit_code"] == 3
    missing = LocalExecutor(tmp_path, approved=True).run(["definitely-not-a-command"])
    assert missing.is_error and "could not start" in missing.content
    assert "truncated" in _cap("x" * 20, 5)
    assert _cap("abc", 5) == "abc"

    with pytest.raises(ValueError, match="argv"):
        _prepare_command(tmp_path, [], ".", 1, None)
    with pytest.raises(ValueError, match="timeout"):
        _prepare_command(tmp_path, ["x"], ".", 0, None)
    with pytest.raises(ValueError, match="escapes"):
        _prepare_command(tmp_path, ["x"], "..", 1, None)
    with pytest.raises(ValueError, match="not a directory"):
        _prepare_command(tmp_path, ["x"], "missing", 1, None)
    with pytest.raises(ValueError, match="Secret-like"):
        _prepare_command(tmp_path, ["x"], ".", 1, {"API_TOKEN": "x"})
    with pytest.raises(ValueError, match="Invalid environment"):
        _prepare_command(tmp_path, ["x"], ".", 1, {"BAD=KEY": "x"})


def test_local_python_execution_ignores_stale_same_timestamp_bytecode(tmp_path: Path):
    module = tmp_path / "stale.py"
    module.write_text("VALUE = 2\n", encoding="utf-8")
    timestamp = int(module.stat().st_mtime)
    os.utime(module, (timestamp, timestamp))
    py_compile.compile(str(module), doraise=True)
    module.write_text("VALUE = 1\n", encoding="utf-8")
    os.utime(module, (timestamp, timestamp))

    result = LocalExecutor(tmp_path, approved=True).run(
        ["python", "-c", "from stale import VALUE; assert VALUE == 1"]
    )
    assert not result.is_error, result.content


def test_local_executor_isolates_versioned_python_executable(tmp_path: Path, monkeypatch):
    observed = {}

    def capture_process(command, cwd, env, timeout_seconds):
        observed.update(env)
        return ToolExecution.ok("ok")

    monkeypatch.setattr(executors, "_run_process", capture_process)
    result = LocalExecutor(tmp_path, approved=True).run(
        ["/usr/bin/python3.12", "-c", "pass"]
    )

    assert not result.is_error
    assert observed.get("PYTHONPYCACHEPREFIX")
    assert observed.get("PYTHONDONTWRITEBYTECODE") == "1"


def test_process_timeout_and_docker_command_is_hardened(tmp_path: Path, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout", 1), output="partial")

    monkeypatch.setattr(executors.subprocess, "run", timeout)
    result = executors._run_process(["x"], tmp_path, {}, 1)
    assert result.is_error and "timed out" in result.content

    calls = []
    monkeypatch.setattr(DockerExecutor, "available", classmethod(lambda cls, binary="docker": (True, "ok")))
    monkeypatch.setattr(DockerExecutor, "image_available", lambda self: True)
    monkeypatch.setattr(
        executors,
        "_run_process",
        lambda command, cwd, env, timeout: calls.append(command) or ToolExecution.ok("ok"),
    )
    result = DockerExecutor(tmp_path).run(["python", "-c", "pass"], env={"CUSTOM": "1"})
    command = calls[0]
    assert not result.is_error
    assert command[command.index("--network") + 1] == "none"
    assert "--read-only" in command and "ALL" in command and "CUSTOM=1" in command
    assert command[command.index("--pids-limit") + 1] == "256"
    assert command[command.index("--memory") + 1] == "2g"
    assert command[command.index("--cpus") + 1] == "2"
    expected_user = (
        f"{os.getuid()}:{os.getgid()}"
        if os.name != "nt" and hasattr(os, "getuid") and os.getuid() != 0
        else "65532:65532"
    )
    assert command[command.index("--user") + 1] == expected_user
    assert "HOME=/tmp" in command
    assert "type=bind" in command[command.index("--mount") + 1]


def test_docker_availability_image_and_executor_selection(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(executors.shutil, "which", lambda value: None)
    assert DockerExecutor.available()[0] is False
    monkeypatch.setattr(executors.shutil, "which", lambda value: value)
    monkeypatch.setattr(executors.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stderr="off", stdout=""))
    assert DockerExecutor.available() == (False, "off")
    assert not DockerExecutor(tmp_path).image_available()

    with pytest.raises(ValueError, match="Unknown executor"):
        choose_executor(tmp_path, preference="bad", docker_image="x", local_approved=False)
    executor, reason = choose_executor(tmp_path, preference="local", docker_image="x", local_approved=False)
    assert executor is None and "not been approved" in reason
    monkeypatch.setattr(DockerExecutor, "available", classmethod(lambda cls: (False, "off")))
    with pytest.raises(ExecutorUnavailable):
        choose_executor(tmp_path, preference="docker", docker_image="x", local_approved=False)
    executor, reason = choose_executor(tmp_path, preference="auto", docker_image="x", local_approved=False)
    assert executor is None and reason == "off"
    executor, reason = choose_executor(tmp_path, preference="auto", docker_image="x", local_approved=True)
    assert isinstance(executor, LocalExecutor) and reason is None
