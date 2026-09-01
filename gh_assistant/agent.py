"""The provider-neutral agent loop with policy, hooks, retry, and checkpoints."""

from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass
from typing import Any, Callable

from gh_assistant.context import ContextCompactor
from gh_assistant.contracts import (
    Message,
    ModelBackend,
    PolicyDecision,
    RunStatus,
    TextPart,
    ToolExecution,
    ToolResultPart,
)
from gh_assistant.hooks import HookBus
from gh_assistant.policy import PermissionPolicy, PolicyContext
from gh_assistant.providers import ProviderFailure
from gh_assistant.state import StateStore
from gh_assistant.tools import ToolRegistry


@dataclass(slots=True)
class AgentRunResult:
    messages: list[Message]
    turns: int
    tool_calls: int
    stop_reason: str
    final_text: str
    paused: bool = False


class AgentLoop:
    def __init__(
        self,
        *,
        backend: ModelBackend,
        registry: ToolRegistry,
        policy: PermissionPolicy,
        policy_context: PolicyContext,
        state: StateStore,
        run_id: str,
        actor: str,
        compactor: ContextCompactor,
        hooks: HookBus | None = None,
        max_turns: int = 30,
        max_tool_calls: int = 120,
        max_tokens: int = 8_000,
        approval_callback: Callable[[dict[str, Any]], bool | None] | None = None,
    ):
        self.backend = backend
        self.registry = registry
        self.policy = policy
        self.policy_context = policy_context
        self.state = state
        self.run_id = run_id
        self.actor = actor
        self.compactor = compactor
        self.hooks = hooks or HookBus()
        self.max_turns = max_turns
        self.max_tool_calls = max_tool_calls
        self.max_tokens = max_tokens
        self.approval_callback = approval_callback

    def run(
        self,
        *,
        system: str,
        messages: list[Message],
        start_turn: int = 0,
        start_tool_calls: int = 0,
        should_stop: Callable[[], bool] | None = None,
        checkpoint_state: Callable[[], dict[str, Any]] | None = None,
    ) -> AgentRunResult:
        turns = start_turn
        tool_count = start_tool_calls
        context_recovered = False
        continuations = 0

        while turns < self.max_turns:
            messages = self.compactor.compact(messages, self.backend)
            turns += 1
            request_payload = {
                "run_id": self.run_id,
                "actor": self.actor,
                "turn": turns,
                "message_count": len(messages),
                "tool_count": len(self.registry.specs),
            }
            self.hooks.emit("before_model", request_payload)
            self.state.append_event(
                self.run_id, "model_requested", self.actor, request_payload
            )
            try:
                response = self._complete_with_retry(system, messages)
            except ProviderFailure as exc:
                self.hooks.emit(
                    "model_error",
                    {**request_payload, "kind": exc.kind, "error": str(exc)},
                )
                self.state.append_event(
                    self.run_id,
                    "model_failed",
                    self.actor,
                    {"kind": exc.kind, "error": str(exc), "turn": turns},
                )
                if exc.kind == "context_overflow" and not context_recovered:
                    messages = self.compactor.reactive(messages, self.backend)
                    context_recovered = True
                    continue
                self._checkpoint(turns, tool_count, messages, checkpoint_state)
                raise

            assistant = response.as_message()
            messages.append(assistant)
            usage_payload = {
                "turn": turns,
                "model": response.model,
                "stop_reason": response.stop_reason,
                "usage": response.usage.to_dict(),
                "tool_calls": [call.name for call in assistant.tool_calls()],
            }
            self.hooks.emit("after_model", usage_payload)
            self.state.append_event(
                self.run_id, "model_completed", self.actor, usage_payload
            )

            calls = assistant.tool_calls()
            if not calls:
                if response.stop_reason in {"max_tokens", "length"} and continuations < 2:
                    continuations += 1
                    messages.append(
                        Message.text(
                            "user",
                            "Output limit reached. Continue directly without recap; "
                            "finish the remaining work in smaller steps.",
                        )
                    )
                    self._checkpoint(turns, tool_count, messages, checkpoint_state)
                    continue
                final_text = assistant.text_content()
                self._checkpoint(turns, tool_count, messages, checkpoint_state)
                self.hooks.emit(
                    "stop",
                    {"run_id": self.run_id, "actor": self.actor, "reason": "end_turn"},
                )
                return AgentRunResult(
                    messages, turns, tool_count, "end_turn", final_text
                )

            results: list[ToolResultPart] = []
            for call in calls:
                tool_count += 1
                if tool_count > self.max_tool_calls:
                    result = ToolExecution.error("Run tool-call budget exhausted")
                else:
                    result, paused = self._execute_tool(call.id, call.name, call.arguments)
                    if paused:
                        results.append(_to_result(call.id, result))
                        messages.append(Message(role="user", parts=results))
                        self._checkpoint(turns, tool_count, messages, checkpoint_state)
                        return AgentRunResult(
                            messages,
                            turns,
                            tool_count,
                            "waiting_approval",
                            "",
                            paused=True,
                        )
                results.append(_to_result(call.id, result))
            messages.append(Message(role="user", parts=results))
            self._checkpoint(turns, tool_count, messages, checkpoint_state)
            if should_stop and should_stop():
                return AgentRunResult(
                    messages, turns, tool_count, "phase_complete", ""
                )

        self._checkpoint(turns, tool_count, messages, checkpoint_state)
        return AgentRunResult(
            messages,
            turns,
            tool_count,
            "max_turns",
            "Agent turn budget exhausted",
        )

    def _complete_with_retry(self, system: str, messages: list[Message]):
        for attempt in range(4):
            try:
                return self.backend.complete(
                    system=system,
                    messages=messages,
                    tools=self.registry.specs,
                    max_tokens=self.max_tokens,
                )
            except ProviderFailure as exc:
                if not exc.retriable or attempt == 3:
                    raise
                delay = exc.retry_after
                if delay is None:
                    base = min(0.5 * (2**attempt), 4.0)
                    delay = base + random.uniform(0, base * 0.25)
                self.state.append_event(
                    self.run_id,
                    "model_retry",
                    self.actor,
                    {"kind": exc.kind, "attempt": attempt + 1, "delay_seconds": delay},
                )
                time.sleep(min(delay, 30.0))
        raise AssertionError("unreachable")

    def _execute_tool(
        self, call_id: str, name: str, arguments: dict[str, Any]
    ) -> tuple[ToolExecution, bool]:
        cached = self.state.completed_tool_call(self.run_id, call_id)
        if cached is not None:
            return ToolExecution(
                content=str(cached.get("content", "")),
                data=cached.get("data"),
                is_error=bool(cached.get("is_error", False)),
            ), False
        spec = self.registry.spec(name)
        if spec is None:
            return ToolExecution.error(f"Unknown tool: {name}"), False
        decision = self.policy.decide(spec, arguments, self.policy_context)
        subject_hash = hashlib.sha256(
            json.dumps(
                {"tool": name, "arguments": arguments}, sort_keys=True, default=str
            ).encode("utf-8")
        ).hexdigest()
        if decision.decision == PolicyDecision.DENY:
            result = ToolExecution.error(f"Permission denied: {decision.reason}")
            self.state.append_event(
                self.run_id,
                "tool_denied",
                self.actor,
                {"tool": name, "reason": decision.reason},
                correlation_id=call_id,
            )
            return result, False
        if decision.decision == PolicyDecision.ASK:
            approvals = self.state.list_approvals(run_id=self.run_id)
            existing = next(
                (
                    item
                    for item in approvals
                    if item["action"] == f"tool:{name}"
                    and item["subject_hash"] == subject_hash
                ),
                None,
            )
            if existing and existing["status"] == "approved":
                pass
            elif existing and existing["status"] == "denied":
                return ToolExecution.error("Permission denied by user"), False
            else:
                approval = existing or self.state.create_approval(
                    self.run_id,
                    f"tool:{name}",
                    subject_hash,
                    {"tool": name, "arguments": arguments, "reason": decision.reason},
                )
                user_decision = self.approval_callback(approval) if self.approval_callback else None
                if user_decision is True:
                    self.state.decide_approval(approval["id"], "approved")
                elif user_decision is False:
                    self.state.decide_approval(approval["id"], "denied")
                    return ToolExecution.error("Permission denied by user"), False
                else:
                    self.state.update_run(
                        self.run_id, status=RunStatus.WAITING_APPROVAL.value
                    )
                    return ToolExecution.error(
                        f"Approval pending: {approval['id']}"
                    ), True

        self.state.begin_tool_call(self.run_id, call_id, name)
        event_payload = {"tool": name, "arguments": arguments}
        self.hooks.emit("before_tool", event_payload)
        self.state.append_event(
            self.run_id,
            "tool_started",
            self.actor,
            event_payload,
            correlation_id=call_id,
        )
        started = time.monotonic()
        result = self.registry.execute(name, arguments)
        duration_ms = int((time.monotonic() - started) * 1000)
        result_payload = {
            "tool": name,
            "content": result.content,
            "data": result.data,
            "is_error": result.is_error,
            "duration_ms": duration_ms,
        }
        self.state.finish_tool_call(
            self.run_id,
            call_id,
            {"content": result.content, "data": result.data, "is_error": result.is_error},
        )
        self.state.append_event(
            self.run_id,
            "tool_failed" if result.is_error else "tool_completed",
            self.actor,
            result_payload,
            correlation_id=call_id,
        )
        self.hooks.emit("after_tool", result_payload)
        return result, False

    def _checkpoint(
        self,
        turns: int,
        tool_calls: int,
        messages: list[Message],
        state_factory: Callable[[], dict[str, Any]] | None,
    ) -> None:
        extra = state_factory() if state_factory else {}
        self.state.save_checkpoint(
            self.run_id,
            turns,
            messages,
            {"tool_calls": tool_calls, **extra},
            actor=self.actor,
        )


def _to_result(call_id: str, result: ToolExecution) -> ToolResultPart:
    return ToolResultPart(
        tool_call_id=call_id,
        content=result.content,
        data=result.data,
        is_error=result.is_error,
    )
