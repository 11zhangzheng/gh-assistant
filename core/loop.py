#!/usr/bin/env python3
"""
core/loop.py — s01: The Agent Loop
(with s02 dispatch + s03 permission gate + s08 compact + s09 memory)

The loop never changes:

    while stop_reason == "tool_use":
        response = LLM(messages, tools)
        execute tools (through the permission gate)
        append results

The model decides when to call tools and when to stop.
The harness (tools, permissions, memory, compaction) plugs in around it:

    loop = AgentLoop(client=..., model=MODEL, system=SYSTEM,
                     tools=registry.schemas, handlers=registry.handlers,
                     permissions=default_policy(interactive=True),
                     memory=memory, compactor=compactor)
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
                 memory=None, compactor=None,
                 max_turns: int = 50, verbose: bool = True):
        self.client = client
        self.model = model
        self.system = system
        self.tools = tools
        self.handlers = handlers
        self.permissions = permissions or PermissionPolicy()
        self.memory = memory          # s09: MemoryStore (or None)
        self.compactor = compactor    # s08: Compactor (or None)
        self.max_turns = max_turns
        self.verbose = verbose
        self._memory_injected = False
        self._reactive_retries = 0

    def run(self, messages: list[dict]) -> list[dict]:
        """Run the loop until the model stops calling tools. Returns messages."""
        # s09: inject relevant repo memory into the first turn (once per session).
        if self.memory and not self._memory_injected:
            self._inject_memory(messages)

        for _ in range(self.max_turns):
            # s08: cheap preprocessors first (budget -> snip -> micro),
            # then LLM compact_history only if still over the threshold.
            if self.compactor:
                self.compactor.before_send(messages)

            try:
                response = self.client.messages.create(
                    model=self.model, system=self.system,
                    messages=messages, tools=self.tools, max_tokens=8000,
                )
            except Exception as e:
                # s08: reactive compact on prompt_too_long, retry once.
                if (self.compactor and _is_prompt_too_long(e)
                        and self._reactive_retries < 1):
                    self._reactive_retries += 1
                    messages[:] = self.compactor.reactive(messages)
                    continue
                raise

            messages.append({"role": "assistant", "content": response.content})

            # If the model didn't call a tool, we're done.
            if response.stop_reason != "tool_use":
                # s09: extract new memories at end of turn.
                if self.memory:
                    n = self.memory.extract(messages)
                    if n and self.verbose:
                        print(f"\033[90m[memory: extracted {n} new memory]\033[0m")
                return messages

            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                # s03: run through the permission gate before executing.
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

    # ── s09: memory injection ───────────────────────────────
    def _inject_memory(self, messages: list[dict]) -> None:
        recent = _recent_text(messages)
        prompt = self.memory.relevant_prompt(recent)
        if prompt.strip():
            messages.insert(0, {"role": "user",
                                "content": f"[Repo memory]\n\n{prompt}"})
        self._memory_injected = True


def _recent_text(messages: list[dict], last: int = 2) -> str:
    text = []
    for m in messages[-last:]:
        c = m.get("content")
        if isinstance(c, str):
            text.append(c)
        elif isinstance(c, list):
            for b in c:
                btype = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
                if btype == "text":
                    text.append(b.text)
    return "\n".join(text)


def _is_prompt_too_long(e: Exception) -> bool:
    s = str(e).lower()
    status = getattr(e, "status_code", None)
    return ("prompt_too_long" in s or "context_length_exceeded" in s
            or "too long" in s or status == 413)


def _short(s: str, limit: int = 200) -> str:
    return s[:limit] + "..." if len(s) > limit else s
