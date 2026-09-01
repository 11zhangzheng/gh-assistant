from __future__ import annotations

from pathlib import Path

from gh_assistant.agent import AgentLoop
from gh_assistant.context import ContextCompactor
from gh_assistant.contracts import (
    Message,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolEffect,
    ToolExecution,
    ToolSpec,
)
from gh_assistant.policy import PermissionPolicy, PolicyContext
from gh_assistant.providers import ScriptedBackend
from gh_assistant.state import StateStore
from gh_assistant.tools import ToolRegistry


def _run_and_state(tmp_path: Path):
    state = StateStore(tmp_path / "state")
    run = state.create_run(
        repo="owner/repo",
        issue_number=1,
        repo_path=tmp_path,
        provider="scripted",
        model="test",
        config={},
    )
    return state, run


def test_loop_continues_on_actual_tool_call_not_stop_reason(tmp_path: Path):
    state, run = _run_and_state(tmp_path)
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            "echo",
            "",
            {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            ToolEffect.READ,
        ),
        lambda value: ToolExecution.ok(value.upper()),
    )
    backend = ScriptedBackend(
        [
            ModelResponse(
                [ToolCallPart("call1", "echo", {"value": "hello"})],
                stop_reason="end_turn",
            ),
            ModelResponse([TextPart("finished")], stop_reason="end_turn"),
        ]
    )
    result = AgentLoop(
        backend=backend,
        registry=registry,
        policy=PermissionPolicy(),
        policy_context=PolicyContext(executor="docker"),
        state=state,
        run_id=run["id"],
        actor="main",
        compactor=ContextCompactor(tmp_path / "artifacts"),
    ).run(system="system", messages=[Message.text("user", "go")])
    assert result.final_text == "finished"
    assert result.tool_calls == 1
    tool_result = result.messages[2].parts[0]
    assert tool_result.content == "HELLO"
    assert any(event["event_type"] == "tool_completed" for event in state.events(run["id"]))


def test_external_tool_pauses_noninteractive_run(tmp_path: Path):
    state, run = _run_and_state(tmp_path)
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            "comment",
            "",
            {"type": "object", "properties": {}, "additionalProperties": False},
            ToolEffect.EXTERNAL_WRITE,
        ),
        lambda: ToolExecution.ok("written"),
    )
    backend = ScriptedBackend(
        [ModelResponse([ToolCallPart("call1", "comment", {})], stop_reason="tool_use")]
    )
    result = AgentLoop(
        backend=backend,
        registry=registry,
        policy=PermissionPolicy(),
        policy_context=PolicyContext(executor="readonly"),
        state=state,
        run_id=run["id"],
        actor="main",
        compactor=ContextCompactor(tmp_path / "artifacts"),
    ).run(system="system", messages=[Message.text("user", "go")])
    assert result.paused is True
    assert state.get_run(run["id"])["status"] == "waiting_approval"
    assert len(state.list_approvals(run_id=run["id"], status="pending")) == 1


def test_completed_tool_call_is_reused_without_handler(tmp_path: Path):
    state, run = _run_and_state(tmp_path)
    called = {"count": 0}
    registry = ToolRegistry()

    def handler():
        called["count"] += 1
        return ToolExecution.ok("fresh")

    registry.register(
        ToolSpec(
            "once",
            "",
            {"type": "object", "properties": {}, "additionalProperties": False},
            ToolEffect.READ,
        ),
        handler,
    )
    state.begin_tool_call(run["id"], "same", "once")
    state.finish_tool_call(run["id"], "same", {"content": "cached", "is_error": False})
    backend = ScriptedBackend(
        [
            ModelResponse([ToolCallPart("same", "once", {})]),
            ModelResponse([TextPart("done")]),
        ]
    )
    result = AgentLoop(
        backend=backend,
        registry=registry,
        policy=PermissionPolicy(),
        policy_context=PolicyContext(executor="docker"),
        state=state,
        run_id=run["id"],
        actor="main",
        compactor=ContextCompactor(tmp_path / "artifacts"),
    ).run(system="system", messages=[Message.text("user", "go")])
    assert called["count"] == 0
    assert result.messages[2].parts[0].content == "cached"

