from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from gh_assistant.agent import AgentLoop
from gh_assistant.config import ProjectConfig, Settings, _normalize_commands, _optional_float, detect_verification, load_dotenv
from gh_assistant.context import ContextCompactor
from gh_assistant.contracts import Message, ModelResponse, TextPart, ToolCallPart, ToolEffect, ToolExecution, ToolSpec
from gh_assistant.policy import PermissionPolicy, PolicyContext
from gh_assistant.providers import (
    AnthropicBackend,
    OpenAIBackend,
    ProviderFailure,
    ScriptedBackend,
    build_backend,
    classify_provider_error,
)
from gh_assistant.state import StateStore
from gh_assistant.tools import ToolRegistry
from gh_assistant import workspace
from gh_assistant.workspace import GitError, WorktreeManager, commit_changes, push_branch


def test_dotenv_settings_validation_and_project_config(tmp_path: Path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text("# comment\nGHA_PROVIDER=openai\nGHA_MODEL='gpt-test'\nEMPTY=\nBADLINE\n", encoding="utf-8")
    monkeypatch.delenv("GHA_PROVIDER", raising=False)
    monkeypatch.delenv("GHA_MODEL", raising=False)
    load_dotenv(dotenv)
    assert os.environ["GHA_PROVIDER"] == "openai"
    monkeypatch.setenv("GHA_PROVIDER", "anthropic")
    load_dotenv(dotenv, override=False)
    assert os.environ["GHA_PROVIDER"] == "anthropic"
    load_dotenv(dotenv, override=True)
    assert os.environ["GHA_PROVIDER"] == "openai"
    load_dotenv(tmp_path / "missing")

    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("GHA_INPUT_COST_PER_MILLION", "1.25")
    settings = Settings.from_env(state_dir=tmp_path / "state")
    assert settings.provider == "openai" and settings.model == "gpt-test"
    assert settings.input_cost_per_million == 1.25
    assert settings.validate_provider() == []
    assert "Unsupported provider" in Settings(provider="bad", model="").validate_provider()[0]
    assert Settings(provider="anthropic", anthropic_api_key="").validate_provider()
    assert Settings(provider="openai", openai_api_key="").validate_provider()
    assert _optional_float("") is None and _optional_float("2") == 2.0

    empty = tmp_path / "empty"
    empty.mkdir()
    assert ProjectConfig.load(empty).verify == []
    (empty / "requirements.txt").write_text("pytest", encoding="utf-8")
    (empty / "tests").mkdir()
    assert detect_verification(empty)[0][:3] == ["python", "-m", "pytest"]
    (empty / ".gha.yaml").write_text(
        "version: 1\nverify: [[python, -c, pass]]\nskills: [test]\ninclude: ['*.py']\nexclude: [build]\n",
        encoding="utf-8",
    )
    config = ProjectConfig.load(empty)
    assert config.skills == ["test"] and config.include == ["*.py"]
    (empty / ".gha.yaml").write_text("verify: nope", encoding="utf-8")
    with pytest.raises(ValueError, match="verify must"):
        ProjectConfig.load(empty)
    (empty / ".gha.yaml").write_text("permissions: allow", encoding="utf-8")
    with pytest.raises(ValueError, match="privileged/unknown"):
        ProjectConfig.load(empty)
    with pytest.raises(ValueError, match="non-empty argv"):
        _normalize_commands([[]])


class Recorder:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    def create(self, **kwargs):
        if self.error:
            raise self.error
        return self.value


def test_provider_build_invalid_responses_and_scripted_paths():
    settings = Settings(provider="anthropic", model="a")
    assert isinstance(build_backend(settings, client=SimpleNamespace(messages=Recorder())), AnthropicBackend)
    settings.provider = "openai"
    assert isinstance(
        build_backend(settings, client=SimpleNamespace(chat=SimpleNamespace(completions=Recorder()))),
        OpenAIBackend,
    )
    settings.provider = "scripted"
    with pytest.raises(ValueError, match="explicit responses"):
        build_backend(settings)

    empty = SimpleNamespace(choices=[], model="gpt", usage={})
    backend = OpenAIBackend(model="gpt", client=SimpleNamespace(chat=SimpleNamespace(completions=Recorder(empty))))
    with pytest.raises(ProviderFailure, match="no choices"):
        backend.complete(system="s", messages=[], tools=[], max_tokens=1)

    invalid = SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(content=None, tool_calls=[SimpleNamespace(
                id="c", function=SimpleNamespace(name="x", arguments="{bad")
            )]),
        )],
        model="gpt",
        usage={},
    )
    result = OpenAIBackend(
        model="gpt", client=SimpleNamespace(chat=SimpleNamespace(completions=Recorder(invalid)))
    ).complete(system="s", messages=[], tools=[], max_tokens=1)
    assert result.parts[0].arguments == {"_invalid_json": "{bad"}

    error = RuntimeError("connection lost")
    with pytest.raises(ProviderFailure) as caught:
        AnthropicBackend(model="a", client=SimpleNamespace(messages=Recorder(error=error))).complete(
            system="s", messages=[], tools=[], max_tokens=1
        )
    assert caught.value.kind == "network"

    called = ScriptedBackend(lambda **request: ModelResponse([TextPart(request["system"])]))
    assert called.complete(system="hello", messages=[], tools=[], max_tokens=1).parts[0].text == "hello"
    exhausted = ScriptedBackend([])
    with pytest.raises(ProviderFailure, match="ran out"):
        exhausted.complete(system="s", messages=[], tools=[], max_tokens=1)


@pytest.mark.parametrize(
    "status,message,kind,retriable",
    [
        (400, "bad", "invalid_request", False),
        (None, "connection reset", "network", True),
        (None, "other", "unknown", False),
        (None, "rate limit", "rate_limit", True),
        (None, "overloaded", "overloaded", True),
        (None, "context_length", "context_overflow", False),
        (None, "authentication failed", "authentication", False),
    ],
)
def test_provider_error_text_classification(status, message, kind, retriable):
    error = RuntimeError(message)
    error.status_code = status
    error.headers = {"retry-after": "1.5"}
    result = classify_provider_error(error)
    assert result.kind == kind and result.retriable is retriable
    if retriable and kind == "rate_limit":
        assert result.retry_after == 1.5


def _loop(tmp_path: Path, backend, registry=None, **kwargs):
    state = StateStore(tmp_path / "state")
    run = state.create_run(
        repo="o/r", issue_number=1, repo_path=tmp_path,
        provider="scripted", model="fake", config={},
    )
    loop = AgentLoop(
        backend=backend,
        registry=registry or ToolRegistry(),
        policy=PermissionPolicy(),
        policy_context=PolicyContext(executor="readonly"),
        state=state,
        run_id=run["id"],
        actor="main",
        compactor=ContextCompactor(tmp_path / "artifacts"),
        **kwargs,
    )
    return state, run, loop


def test_agent_retry_continuation_budget_and_context_recovery(tmp_path: Path, monkeypatch):
    attempts = {"n": 0}

    def responses(**request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ProviderFailure("overloaded", "busy", retriable=True, retry_after=0)
        if attempts["n"] == 2:
            return ModelResponse([TextPart("partial")], stop_reason="max_tokens")
        return ModelResponse([TextPart("done")])

    monkeypatch.setattr("gh_assistant.agent.time.sleep", lambda value: None)
    state, run, loop = _loop(tmp_path, ScriptedBackend(responses), max_turns=4)
    result = loop.run(system="s", messages=[Message.text("user", "go")])
    assert result.final_text == "done"
    assert any(e["event_type"] == "model_retry" for e in state.events(run["id"]))

    state2, run2, short = _loop(tmp_path / "short", ScriptedBackend([ModelResponse([TextPart("x")], stop_reason="max_tokens")]), max_turns=1)
    assert short.run(system="s", messages=[]).stop_reason == "max_turns"

    failures = {"n": 0}
    def overflow(**request):
        failures["n"] += 1
        if failures["n"] == 1:
            raise ProviderFailure("context_overflow", "large")
        return ModelResponse([TextPart("recovered")])
    state3, run3, recovered = _loop(tmp_path / "overflow", ScriptedBackend(overflow))
    assert recovered.run(system="s", messages=[Message.text("user", "x")]).final_text == "recovered"


def test_agent_unknown_denied_approval_and_tool_budget(tmp_path: Path):
    registry = ToolRegistry()
    registry.register(
        ToolSpec("external", "", {"type": "object", "properties": {}, "additionalProperties": False}, ToolEffect.EXTERNAL_WRITE),
        lambda: ToolExecution.ok("written"),
    )
    script = ScriptedBackend([
        ModelResponse([ToolCallPart("u", "missing", {}), ToolCallPart("e", "external", {})]),
        ModelResponse([TextPart("done")]),
    ])
    state, run, loop = _loop(tmp_path, script, registry, approval_callback=lambda approval: False)
    result = loop.run(system="s", messages=[])
    assert result.final_text == "done"
    contents = [part.content for part in result.messages[1].parts]
    assert "Unknown tool" in contents[0] and "denied by user" in contents[1]

    budget_script = ScriptedBackend([
        ModelResponse([ToolCallPart("a", "external", {}), ToolCallPart("b", "external", {})]),
        ModelResponse([TextPart("done")]),
    ])
    _, _, budget = _loop(tmp_path / "budget", budget_script, registry, max_tool_calls=0)
    result = budget.run(system="s", messages=[])
    assert all("budget exhausted" in part.content for part in result.messages[1].parts)


def test_workspace_recovery_push_and_failure_paths(git_repo: Path, tmp_path: Path, monkeypatch):
    manager = WorktreeManager(tmp_path / "state")
    worktree = manager.create(
        repo_path=git_repo, repo_slug="o/r", issue_number=2, run_id="run_edge", default_branch="main"
    )
    run = {
        "worktree_path": str(worktree.path), "repo_path": str(git_repo),
        "branch": worktree.branch, "base_ref": worktree.base_ref, "base_sha": worktree.base_sha,
    }
    assert manager.recover(run).branch == worktree.branch
    with pytest.raises(GitError, match="already exists"):
        manager.create(repo_path=git_repo, repo_slug="o/r", issue_number=2, run_id="run_edge")
    bad = dict(run, branch="other")
    with pytest.raises(GitError, match="branch changed"):
        manager.recover(bad)
    with pytest.raises(GitError, match="no changes"):
        commit_changes(worktree.path, issue_number=2, run_id="run_edge")
    with pytest.raises(GitError, match="required to push"):
        push_branch(worktree.path, repo_slug="o/r", branch="x", github_token="")
    with pytest.raises(GitError, match="Invalid GitHub"):
        push_branch(worktree.path, repo_slug="bad", branch="x", github_token="token")

    calls = []
    original_run_git = workspace._run_git
    monkeypatch.setattr(workspace, "_run_git", lambda cwd, args, env: calls.append((args, env)) or "")
    assert push_branch(worktree.path, repo_slug="o/r", branch="branch", github_token="token") == "branch"
    assert calls[0][1]["GIT_ASKPASS_REQUIRE"] == "force"
    monkeypatch.setattr(workspace, "_run_git", original_run_git)

    monkeypatch.setattr(
        workspace.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stderr="bad", stdout=""),
    )
    with pytest.raises(GitError, match="failed: bad"):
        workspace._run_git(git_repo, ["status"], env={})
