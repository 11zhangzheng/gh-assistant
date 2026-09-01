from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gh_assistant.contracts import (
    Message,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolEffect,
    ToolResultPart,
    ToolSpec,
    messages_from_json,
    messages_to_json,
)
from gh_assistant.providers import (
    AnthropicBackend,
    OpenAIBackend,
    ProviderFailure,
    classify_provider_error,
)


class Recorder:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def test_message_roundtrip_preserves_typed_parts():
    messages = [
        Message(
            "assistant",
            [TextPart("thinking result"), ToolCallPart("c1", "read_file", {"path": "a.py"})],
        ),
        Message("user", [ToolResultPart("c1", "contents", data={"lines": 1})]),
    ]
    restored = messages_from_json(messages_to_json(messages))
    assert restored == messages
    assert restored[0].tool_calls()[0].arguments == {"path": "a.py"}


def test_anthropic_adapter_maps_tool_messages_and_usage():
    response = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="done"),
            SimpleNamespace(type="tool_use", id="next", name="echo", input={"x": 1}),
        ],
        stop_reason="tool_use",
        model="claude-test",
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            cache_read_input_tokens=5,
            cache_creation_input_tokens=2,
        ),
    )
    recorder = Recorder(response)
    backend = AnthropicBackend(
        model="claude-test",
        client=SimpleNamespace(messages=recorder),
        input_cost_per_million=1,
        output_cost_per_million=2,
    )
    result = backend.complete(
        system="system",
        messages=[
            Message("assistant", [ToolCallPart("c1", "echo", {"value": "x"})]),
            Message("user", [ToolResultPart("c1", "ok")]),
        ],
        tools=[
            ToolSpec(
                "echo",
                "Echo input",
                {"type": "object", "properties": {}, "additionalProperties": False},
                ToolEffect.READ,
            )
        ],
        max_tokens=1000,
    )
    assert recorder.kwargs["messages"][0]["content"][0]["type"] == "tool_use"
    assert recorder.kwargs["messages"][1]["content"][0]["type"] == "tool_result"
    assert result.parts[1] == ToolCallPart("next", "echo", {"x": 1})
    assert result.usage.cache_read_tokens == 5
    assert result.usage.estimated_cost_usd == pytest.approx(0.000135)


def test_openai_adapter_maps_function_calls_and_tool_results():
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content="checking",
                    tool_calls=[
                        SimpleNamespace(
                            id="call2",
                            function=SimpleNamespace(name="echo", arguments='{"value": 2}'),
                        )
                    ],
                ),
            )
        ],
        model="gpt-test",
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            prompt_tokens_details=SimpleNamespace(cached_tokens=3),
        ),
    )
    recorder = Recorder(response)
    client = SimpleNamespace(chat=SimpleNamespace(completions=recorder))
    backend = OpenAIBackend(model="gpt-test", client=client)
    result = backend.complete(
        system="system",
        messages=[
            Message("assistant", [ToolCallPart("c1", "echo", {"value": 1})]),
            Message("user", [ToolResultPart("c1", "one")]),
        ],
        tools=[],
        max_tokens=100,
    )
    wire = recorder.kwargs["messages"]
    assert wire[0] == {"role": "system", "content": "system"}
    assert wire[1]["tool_calls"][0]["function"]["arguments"] == '{"value": 1}'
    assert wire[2] == {"role": "tool", "tool_call_id": "c1", "content": "one"}
    assert result.parts[-1] == ToolCallPart("call2", "echo", {"value": 2})
    assert result.usage.cache_read_tokens == 3


@pytest.mark.parametrize(
    ("status", "message", "kind", "retriable"),
    [
        (429, "rate limited", "rate_limit", True),
        (529, "overloaded", "overloaded", True),
        (413, "prompt too long", "context_overflow", False),
        (401, "bad auth", "authentication", False),
    ],
)
def test_provider_error_classification(status, message, kind, retriable):
    error = RuntimeError(message)
    error.status_code = status
    classified = classify_provider_error(error)
    assert isinstance(classified, ProviderFailure)
    assert classified.kind == kind
    assert classified.retriable is retriable

