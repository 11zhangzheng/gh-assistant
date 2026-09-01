"""Small synchronous lifecycle hook bus."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any


Hook = Callable[[dict[str, Any]], None]


class HookBus:
    EVENTS = {
        "before_model",
        "after_model",
        "model_error",
        "before_tool",
        "after_tool",
        "phase_changed",
        "stop",
    }

    def __init__(self) -> None:
        self._hooks: dict[str, list[Hook]] = defaultdict(list)

    def register(self, event: str, callback: Hook) -> None:
        if event not in self.EVENTS:
            raise ValueError(f"Unknown hook event: {event}")
        self._hooks[event].append(callback)

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        if event not in self.EVENTS:
            raise ValueError(f"Unknown hook event: {event}")
        for callback in tuple(self._hooks[event]):
            callback(payload)

