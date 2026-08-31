#!/usr/bin/env python3
"""
core/tools.py — s02: Tool Registry (the dispatch map)

One tool = one handler. Adding a tool means registering one handler
and one schema; the loop never changes.

    reg = ToolRegistry()
    reg.register("list_issues", desc, props, required, handler)
    tools    = reg.schemas      # → TOOLS list for the API
    handlers = reg.handlers     # → dispatch map for the loop
"""
from __future__ import annotations

from typing import Callable


class ToolRegistry:
    def __init__(self):
        self._schemas: list[dict] = []
        self._handlers: dict[str, Callable] = {}

    def register(self, name: str, description: str, properties: dict,
                 required: list[str], handler: Callable) -> None:
        self._schemas.append({
            "name": name,
            "description": description,
            "input_schema": {"type": "object", "properties": properties,
                             "required": required},
        })
        self._handlers[name] = handler

    @property
    def schemas(self) -> list[dict]:
        return self._schemas

    @property
    def handlers(self) -> dict[str, Callable]:
        return self._handlers
