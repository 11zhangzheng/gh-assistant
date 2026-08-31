#!/usr/bin/env python3
"""
core/loop.py — s01: The Agent Loop
(with s02 dispatch + s03 permission gate, like s03/code.py)

The loop never changes:

    while stop_reason == "tool_use":
        response = LLM(messages, tools)
        execute tools (through the permission gate)
        append results

The model decides when to call tools and when to stop.
The harness (tools, permissions, memory, compaction) plugs in around it.

    from core.loop import AgentLoop
    loop = AgentLoop(client=..., model=MODEL, system=SYSTEM,
                     tools=registry.schemas, handlers=registry.handlers,
                     permissions=default_policy(interactive=True))
    messages = loop.run([{"role": "user", "content": "..."}])
"""
from __future__ import annotations

from typing import Callable

from core.permissions import PermissionPolicy


class AgentLoop:
    """One agent loop. Same shape as s01/s02/s03, parameterized by the harness."""

    def __init__(self, *, client, model: str, system: str,
                 tools: list[dict], handlers: dict[str, Callable],
                 permissions: PermissionPolicy | None = None,
                 max_turns: int = 50, verbose: bool = True):
        self.client = client
        self.model = model
        self.system = system
        self.tools = tools
        self.handlers = handlers
        self.permissions = permissions or PermissionPolicy()
        self.max_turns = max_turns
        self.verbose = verbose

    def run(self, messages: list[dict]) -> list[dict]:
        """Run the loop until the model stops calling tools. Returns messages."""
        for _ in range(self.max_turns):
            response = self.client.messages.create(
                model=self.model, system=self.system,
                messages=messages, tools=self.tools, max_tokens=8000,
            )
            messages.append({"role": "assistant", "content": response.content})

            # If the model didn't call a tool, we're done
            if response.stop_reason != "tool_use":
                return messages

            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                # s03: run through the permission gate before executing
                if not self.permissions.check(block.name, block.input):
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Permission denied.",
                    })
                    continue

                handler = self.handlers.get(block.name)
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                if self.verbose:
                    print(f"\033[36m> {block.name}{_short(str(block.input))}\033[0m")
                    print(f"\033[90m{_short(str(output))}\033[0m")
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                })

            messages.append({"role": "user", "content": results})

        print("Warning: hit max_turns without finishing.")
        return messages


def _short(s: str, limit: int = 200) -> str:
    return s[:limit] + "..." if len(s) > limit else s
