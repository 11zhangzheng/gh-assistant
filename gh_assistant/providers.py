"""Anthropic, OpenAI-compatible, and deterministic model backends."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from gh_assistant.config import Settings
from gh_assistant.contracts import (
    Message,
    ModelResponse,
    ModelUsage,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
    new_id,
)


@dataclass(slots=True)
class ProviderFailure(RuntimeError):
    kind: str
    message: str
    retriable: bool = False
    retry_after: float | None = None

    def __str__(self) -> str:
        return self.message


class AnthropicBackend:
    provider = "anthropic"

    def __init__(
        self,
        *,
        model: str,
        api_key: str = "",
        base_url: str = "",
        client: Any = None,
        input_cost_per_million: float | None = None,
        output_cost_per_million: float | None = None,
    ):
        self.model = model
        self.input_cost_per_million = input_cost_per_million
        self.output_cost_per_million = output_cost_per_million
        if client is None:
            try:
                from anthropic import Anthropic
            except ImportError as exc:
                raise RuntimeError(
                    "Anthropic provider requires `pip install -e .`"
                ) from exc
            kwargs: dict[str, Any] = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            client = Anthropic(**kwargs)
        self.client = client

    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
    ) -> ModelResponse:
        try:
            response = self.client.messages.create(
                model=self.model,
                system=system,
                messages=_to_anthropic_messages(messages),
                tools=[
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "input_schema": tool.input_schema,
                    }
                    for tool in tools
                ],
                max_tokens=max_tokens,
            )
        except Exception as exc:
            raise classify_provider_error(exc) from exc
        parts = []
        for block in _get(response, "content", []) or []:
            block_type = _get(block, "type", "")
            if block_type == "text":
                parts.append(TextPart(text=str(_get(block, "text", ""))))
            elif block_type == "tool_use":
                parts.append(
                    ToolCallPart(
                        id=str(_get(block, "id", new_id("call_"))),
                        name=str(_get(block, "name", "")),
                        arguments=dict(_get(block, "input", {}) or {}),
                    )
                )
        usage_obj = _get(response, "usage", {}) or {}
        usage = ModelUsage(
            input_tokens=int(_get(usage_obj, "input_tokens", 0) or 0),
            output_tokens=int(_get(usage_obj, "output_tokens", 0) or 0),
            cache_read_tokens=int(_get(usage_obj, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(_get(usage_obj, "cache_creation_input_tokens", 0) or 0),
        )
        usage.estimated_cost_usd = _estimate_cost(
            usage, self.input_cost_per_million, self.output_cost_per_million
        )
        return ModelResponse(
            parts=parts,
            stop_reason=str(_get(response, "stop_reason", "end_turn") or "end_turn"),
            usage=usage,
            model=str(_get(response, "model", self.model) or self.model),
        )


class OpenAIBackend:
    provider = "openai"

    def __init__(
        self,
        *,
        model: str,
        api_key: str = "",
        base_url: str = "",
        client: Any = None,
        input_cost_per_million: float | None = None,
        output_cost_per_million: float | None = None,
    ):
        self.model = model
        self.input_cost_per_million = input_cost_per_million
        self.output_cost_per_million = output_cost_per_million
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError(
                    "OpenAI provider requires `pip install -e .`"
                ) from exc
            kwargs: dict[str, Any] = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            client = OpenAI(**kwargs)
        self.client = client

    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
    ) -> ModelResponse:
        wire_messages = [{"role": "system", "content": system}]
        wire_messages.extend(_to_openai_messages(messages))
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=wire_messages,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.input_schema,
                        },
                    }
                    for tool in tools
                ],
                max_tokens=max_tokens,
            )
        except Exception as exc:
            raise classify_provider_error(exc) from exc
        choices = _get(response, "choices", []) or []
        if not choices:
            raise ProviderFailure("invalid_response", "OpenAI response had no choices")
        choice = choices[0]
        message = _get(choice, "message", {}) or {}
        parts = []
        content = _get(message, "content", "")
        if content:
            parts.append(TextPart(text=str(content)))
        for tool_call in _get(message, "tool_calls", []) or []:
            function = _get(tool_call, "function", {}) or {}
            raw_arguments = _get(function, "arguments", "{}") or "{}"
            try:
                arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else dict(raw_arguments)
            except json.JSONDecodeError:
                arguments = {"_invalid_json": raw_arguments}
            parts.append(
                ToolCallPart(
                    id=str(_get(tool_call, "id", new_id("call_"))),
                    name=str(_get(function, "name", "")),
                    arguments=arguments,
                )
            )
        usage_obj = _get(response, "usage", {}) or {}
        prompt_details = _get(usage_obj, "prompt_tokens_details", {}) or {}
        usage = ModelUsage(
            input_tokens=int(_get(usage_obj, "prompt_tokens", 0) or 0),
            output_tokens=int(_get(usage_obj, "completion_tokens", 0) or 0),
            cache_read_tokens=int(_get(prompt_details, "cached_tokens", 0) or 0),
        )
        usage.estimated_cost_usd = _estimate_cost(
            usage, self.input_cost_per_million, self.output_cost_per_million
        )
        return ModelResponse(
            parts=parts,
            stop_reason=str(_get(choice, "finish_reason", "stop") or "stop"),
            usage=usage,
            model=str(_get(response, "model", self.model) or self.model),
        )


class ScriptedBackend:
    """Deterministic backend for tests, demos, and CI without API spend."""

    provider = "scripted"

    def __init__(
        self,
        responses: Iterable[ModelResponse] | Callable[..., ModelResponse],
        model: str = "scripted-model",
    ):
        self.model = model
        self._callable = responses if callable(responses) else None
        self._responses = iter(()) if callable(responses) else iter(responses)
        self.requests: list[dict[str, Any]] = []

    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
    ) -> ModelResponse:
        request = {
            "system": system,
            "messages": [message.to_dict() for message in messages],
            "tools": [tool.to_dict() for tool in tools],
            "max_tokens": max_tokens,
        }
        self.requests.append(request)
        if self._callable:
            return self._callable(**request)
        try:
            return next(self._responses)
        except StopIteration as exc:
            raise ProviderFailure(
                "script_exhausted", "Scripted backend ran out of responses"
            ) from exc


def build_backend(settings: Settings, *, client: Any = None):
    common = {
        "model": settings.model,
        "input_cost_per_million": settings.input_cost_per_million,
        "output_cost_per_million": settings.output_cost_per_million,
        "client": client,
    }
    if settings.provider == "anthropic":
        return AnthropicBackend(
            api_key=settings.anthropic_api_key,
            base_url=settings.anthropic_base_url,
            **common,
        )
    if settings.provider == "openai":
        return OpenAIBackend(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            **common,
        )
    raise ValueError("ScriptedBackend must be constructed with explicit responses")


def classify_provider_error(exc: Exception) -> ProviderFailure:
    status = _get(exc, "status_code", None)
    text = str(exc)
    low = text.lower()
    headers = _get(exc, "headers", {}) or {}
    retry_after = None
    try:
        value = _get(headers, "retry-after", None) or headers.get("retry-after")
        retry_after = float(value) if value else None
    except (TypeError, ValueError, AttributeError):
        pass
    if status == 429 or "rate limit" in low:
        return ProviderFailure("rate_limit", text, retriable=True, retry_after=retry_after)
    if status in {500, 502, 503, 529} or "overload" in low:
        return ProviderFailure("overloaded", text, retriable=True, retry_after=retry_after)
    if status == 413 or "prompt_too_long" in low or "context_length" in low:
        return ProviderFailure("context_overflow", text, retriable=False)
    if status in {401, 403} or "authentication" in low:
        return ProviderFailure("authentication", text, retriable=False)
    if status in {400, 404, 422}:
        return ProviderFailure("invalid_request", text, retriable=False)
    if any(term in low for term in ("timeout", "connection", "temporarily unavailable")):
        return ProviderFailure("network", text, retriable=True)
    return ProviderFailure("unknown", text, retriable=False)


def _to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out = []
    for message in messages:
        content = []
        for part in message.parts:
            if isinstance(part, TextPart):
                content.append({"type": "text", "text": part.text})
            elif isinstance(part, ToolCallPart):
                content.append(
                    {
                        "type": "tool_use",
                        "id": part.id,
                        "name": part.name,
                        "input": part.arguments,
                    }
                )
            elif isinstance(part, ToolResultPart):
                content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": part.tool_call_id,
                        "content": part.content,
                        "is_error": part.is_error,
                    }
                )
        out.append({"role": message.role, "content": content})
    return out


def _to_openai_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        tool_results = [part for part in message.parts if isinstance(part, ToolResultPart)]
        non_results = [part for part in message.parts if not isinstance(part, ToolResultPart)]
        if non_results:
            text = "\n".join(
                part.text for part in non_results if isinstance(part, TextPart)
            ) or None
            wire: dict[str, Any] = {"role": message.role, "content": text}
            calls = [part for part in non_results if isinstance(part, ToolCallPart)]
            if calls:
                wire["tool_calls"] = [
                    {
                        "id": part.id,
                        "type": "function",
                        "function": {
                            "name": part.name,
                            "arguments": json.dumps(part.arguments, ensure_ascii=False),
                        },
                    }
                    for part in calls
                ]
            out.append(wire)
        for result in tool_results:
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": result.tool_call_id,
                    "content": result.content,
                }
            )
    return out


def _estimate_cost(
    usage: ModelUsage,
    input_rate: float | None,
    output_rate: float | None,
) -> float | None:
    if input_rate is None or output_rate is None:
        return None
    uncached_input = max(0, usage.input_tokens - usage.cache_read_tokens)
    return round(
        (uncached_input * input_rate + usage.output_tokens * output_rate) / 1_000_000,
        8,
    )


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)

