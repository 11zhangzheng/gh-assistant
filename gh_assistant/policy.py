"""Deterministic permission policy. Unknown capabilities fail closed."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gh_assistant.contracts import PolicyDecision, ToolEffect, ToolSpec


@dataclass(slots=True)
class PolicyContext:
    executor: str = "docker"
    local_execution_approved: bool = False
    allowed_external_tools: set[str] = field(default_factory=set)


@dataclass(slots=True)
class PolicyResult:
    decision: PolicyDecision
    reason: str


class PermissionPolicy:
    """Effect-based policy with deny > ask > allow semantics."""

    def decide(
        self,
        tool: ToolSpec,
        arguments: dict[str, Any],
        context: PolicyContext,
    ) -> PolicyResult:
        del arguments  # Reserved for future argument-level restrictions.
        if tool.effect == ToolEffect.DESTRUCTIVE:
            return PolicyResult(PolicyDecision.DENY, "Destructive actions are disabled")
        if tool.effect == ToolEffect.EXTERNAL_WRITE:
            if tool.name in context.allowed_external_tools:
                return PolicyResult(PolicyDecision.ALLOW, "Approved external action")
            return PolicyResult(PolicyDecision.ASK, "Writes to an external system")
        if tool.effect == ToolEffect.COMMAND:
            if context.executor == "docker":
                return PolicyResult(PolicyDecision.ALLOW, "Command runs in Docker sandbox")
            if context.executor == "local" and context.local_execution_approved:
                return PolicyResult(PolicyDecision.ALLOW, "Local executor approved for this run")
            return PolicyResult(PolicyDecision.ASK, "Local execution is not sandboxed")
        if tool.effect in {ToolEffect.READ, ToolEffect.WORKSPACE_WRITE}:
            return PolicyResult(PolicyDecision.ALLOW, "Confined to the run workspace")
        return PolicyResult(PolicyDecision.DENY, "Unknown tool effect")

