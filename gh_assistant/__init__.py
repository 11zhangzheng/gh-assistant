"""gh-assistant: an auditable GitHub SWE agent harness."""

from gh_assistant.contracts import (
    Message,
    ModelResponse,
    ModelUsage,
    RunPhase,
    RunStatus,
    TextPart,
    ToolCallPart,
    ToolEffect,
    ToolResultPart,
)

__all__ = [
    "Message",
    "ModelResponse",
    "ModelUsage",
    "RunPhase",
    "RunStatus",
    "TextPart",
    "ToolCallPart",
    "ToolEffect",
    "ToolResultPart",
]

__version__ = "0.2.0"

