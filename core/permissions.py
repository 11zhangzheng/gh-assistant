#!/usr/bin/env python3
"""
core/permissions.py — s03: Permission System (three-gate pipeline)

For gh-assistant the permission layer is safety-critical: this agent
writes to real GitHub repos. Three gates before every tool call:

    Gate 1: Hard deny    — tool can never run
    Gate 2: Rule match   — data-driven allow / ask / deny
    Gate 3: User prompt  — interactive approval (non-interactive => deny)

Rules are data, not code. `default_policy()` is the starting policy:
read is free, writing to a repo asks, high-risk ops ask too.
"""
from __future__ import annotations

from typing import Callable


class PermissionRule:
    """One rule: for `tools`, take `action` (allow/ask/deny), optionally gated by `match(args)`."""

    def __init__(self, tools: list[str], action: str = "ask",
                 match: Callable[[dict], bool] | None = None,
                 message: str = ""):
        self.tools = tools
        self.action = action        # "allow" | "ask" | "deny"
        self.match = match          # optional predicate on args
        self.message = message


class PermissionPolicy:
    """Three-gate pipeline. check() returns True if the call may proceed."""

    def __init__(self, rules: list[PermissionRule] | None = None,
                 hard_deny: list[str] | None = None,
                 interactive: bool = True):
        self.rules = rules or []
        self.hard_deny = hard_deny or []
        self.interactive = interactive

    def check(self, tool_name: str, args: dict) -> bool:
        # Gate 1: hard deny — never runs
        if tool_name in self.hard_deny:
            print(f"\033[31m⛔ Hard deny: {tool_name}\033[0m")
            return False

        # Gate 2: rule match — first matching rule wins
        for rule in self.rules:
            if tool_name in rule.tools and (rule.match is None or rule.match(args)):
                return self._decide(rule.action, tool_name, args, rule.message)

        # No rule matched → allow (read-only tools, future tools)
        return True

    # ── gates 2/3 internals ────────────────────────────────
    def _decide(self, action: str, tool_name: str, args: dict, message: str) -> bool:
        if action == "allow":
            return True
        if action == "deny":
            print(f"\033[31m⛔ Denied: {message or tool_name}\033[0m")
            return False
        return self._ask_user(tool_name, args, message)  # action == "ask"

    def _ask_user(self, tool_name: str, args: dict, message: str) -> bool:
        if not self.interactive:
            print(f"\033[31m⛔ {message or tool_name} requires approval; "
                  f"non-interactive → denied\033[0m")
            return False
        print(f"\n\033[33m⚠  {message or 'Tool requires approval'}\033[0m")
        print(f"   Tool: {tool_name}({_short(str(args))})")
        choice = input("   Allow? [y/N] ").strip().lower()
        return choice in ("y", "yes")


def default_policy(interactive: bool = True) -> PermissionPolicy:
    """gh-assistant's starting policy: read is free, write needs approval."""
    return PermissionPolicy(
        hard_deny=[
            # Merging rewrites the default branch — never allowed implicitly.
            "merge_pull_request",
        ],
        rules=[
            # Read-only GitHub ops auto-allow.
            PermissionRule(
                tools=["repo_info", "list_issues", "get_issue", "list_labels"],
                action="allow",
            ),
            # Writing to a repo (labels, comments) asks in interactive mode.
            PermissionRule(
                tools=["add_labels", "comment_on_issue"],
                action="ask",
                message="Writes to the repository",
            ),
            # High-risk ops ask too.
            PermissionRule(
                tools=["close_issue", "open_pull_request"],
                action="ask",
                message="High-risk repository operation",
            ),
        ],
        interactive=interactive,
    )


def _short(s: str, limit: int = 120) -> str:
    return s[:limit] + "..." if len(s) > limit else s
