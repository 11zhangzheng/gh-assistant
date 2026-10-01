"""Provider-neutral contracts shared by every harness component."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol, TypeAlias


class ToolEffect(StrEnum):
    READ = "read"
    WORKSPACE_WRITE = "workspace_write"
    COMMAND = "command"
    EXTERNAL_WRITE = "external_write"
    DESTRUCTIVE = "destructive"


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class RunPhase(StrEnum):
    INTAKE = "intake"
    PLANNING = "planning"
    IMPLEMENTATION = "implementation"
    VERIFICATION = "verification"
    REVIEW = "review"
    REPAIR = "repair"
    PUBLISH = "publish"
    DONE = "done"


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    READY_TO_PUBLISH = "ready_to_publish"
    PUBLISHED = "published"
    COMPLETED_LOCAL = "completed_local"
    SUCCEEDED = "succeeded"
    NEEDS_HUMAN = "needs_human"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABSTAINED = "abstained"


@dataclass(slots=True)
class TextPart:
    text: str
    kind: Literal["text"] = "text"


@dataclass(slots=True)
class ToolCallPart:
    id: str
    name: str
    arguments: dict[str, Any]
    kind: Literal["tool_call"] = "tool_call"


@dataclass(slots=True)
class ToolResultPart:
    tool_call_id: str
    content: str
    is_error: bool = False
    data: dict[str, Any] | list[Any] | None = None
    kind: Literal["tool_result"] = "tool_result"


MessagePart: TypeAlias = TextPart | ToolCallPart | ToolResultPart


@dataclass(slots=True)
class Message:
    role: Literal["user", "assistant"]
    parts: list[MessagePart]

    @classmethod
    def text(cls, role: Literal["user", "assistant"], text: str) -> "Message":
        return cls(role=role, parts=[TextPart(text=text)])

    def text_content(self) -> str:
        return "\n".join(part.text for part in self.parts if isinstance(part, TextPart))

    def tool_calls(self) -> list[ToolCallPart]:
        return [part for part in self.parts if isinstance(part, ToolCallPart)]

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "parts": [asdict(part) for part in self.parts]}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Message":
        parts: list[MessagePart] = []
        for raw in value.get("parts", []):
            kind = raw.get("kind")
            if kind == "text":
                parts.append(TextPart(text=str(raw.get("text", ""))))
            elif kind == "tool_call":
                parts.append(ToolCallPart(
                    id=str(raw["id"]),
                    name=str(raw["name"]),
                    arguments=dict(raw.get("arguments") or {}),
                ))
            elif kind == "tool_result":
                parts.append(ToolResultPart(
                    tool_call_id=str(raw["tool_call_id"]),
                    content=str(raw.get("content", "")),
                    is_error=bool(raw.get("is_error", False)),
                    data=raw.get("data"),
                ))
            else:
                raise ValueError(f"Unknown message part kind: {kind!r}")
        return cls(role=value["role"], parts=parts)


@dataclass(slots=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    estimated_cost_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ModelResponse:
    parts: list[MessagePart]
    stop_reason: str = "end_turn"
    usage: ModelUsage = field(default_factory=ModelUsage)
    model: str = ""

    def as_message(self) -> Message:
        return Message(role="assistant", parts=self.parts)


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    effect: ToolEffect

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "effect": self.effect.value,
        }


@dataclass(slots=True)
class ToolExecution:
    content: str
    data: dict[str, Any] | list[Any] | None = None
    is_error: bool = False

    @classmethod
    def ok(cls, content: str, data: dict[str, Any] | list[Any] | None = None) -> "ToolExecution":
        return cls(content=content, data=data)

    @classmethod
    def error(cls, content: str, data: dict[str, Any] | None = None) -> "ToolExecution":
        return cls(content=content, data=data, is_error=True)


class ModelBackend(Protocol):
    provider: str
    model: str

    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
    ) -> ModelResponse: ...


class Executor(Protocol):
    name: str

    def run(
        self,
        argv: list[str],
        *,
        cwd: str = ".",
        timeout_seconds: int = 120,
        env: dict[str, str] | None = None,
    ) -> ToolExecution: ...


def new_id(prefix: str = "") -> str:
    suffix = uuid.uuid4().hex[:12]
    return f"{prefix}{suffix}" if prefix else suffix


def messages_to_json(messages: list[Message]) -> str:
    return json.dumps([message.to_dict() for message in messages], ensure_ascii=False)


def messages_from_json(raw: str) -> list[Message]:
    return [Message.from_dict(value) for value in json.loads(raw or "[]")]
