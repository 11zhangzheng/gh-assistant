#!/usr/bin/env python3
"""
cli.py — gh-assistant entry point.

Usage:
    python cli.py ping <repo>                 # check GitHub connectivity
    python cli.py triage <repo> [--limit N]   # agent triages open issues
    python cli.py interactive [repo]          # REPL over the agent loop

Milestone 1 wired in: skills (s07) + compact (s08) + memory (s09).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

# allow `python cli.py` from anywhere inside gh-assistant/
sys.path.insert(0, str(Path(__file__).parent))

from core.loop import AgentLoop
from core.permissions import default_policy
from context.compact import Compactor
from context.skills import SkillRegistry
from github.api import GitHubClient
from github.tools import build_github_tools
from memory.memory import MemoryStore

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# Windows console may default to GBK; force UTF-8 so emoji/ANSI output won't crash.
# (VS Code terminal and Windows Terminal are UTF-8 and will render it correctly.)
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

MODEL = os.getenv("MODEL_ID", "claude-sonnet-4-6")


# ── build the harness ─────────────────────────────────────
def build_loop(repo: str, interactive: bool) -> AgentLoop:
    llm = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
    registry = build_github_tools(GitHubClient())

    # s07: two-level skill loading (catalog in system prompt, content via load_skill).
    skills = SkillRegistry(Path(__file__).parent / "skills")
    registry.register(
        "load_skill", "Load a skill's full instructions into context.",
        {"name": {"type": "string"}}, ["name"], skills.load_skill,
    )

    # s09: per-repo memory store (data lives under .memory/, gitignored).
    memory = MemoryStore(Path(".memory") / repo.replace("/", "_"),
                         client=llm, model=MODEL)

    # s08: four-layer compaction pipeline.
    compactor = Compactor(client=llm, model=MODEL,
                          persist_dir=Path(".task_outputs/tool-results"),
                          transcript_dir=Path(".transcripts"))

    return AgentLoop(
        client=llm,
        model=MODEL,
        system=_build_system(repo, skills, memory),
        tools=registry.schemas,
        handlers=registry.handlers,
        permissions=default_policy(interactive=interactive),
        memory=memory,
        compactor=compactor,
    )


def _build_system(repo: str, skills: SkillRegistry, memory: MemoryStore) -> str:
    return (
        f"You are gh-assistant, a GitHub repository maintenance agent.\n"
        f"Working repo: {repo}\n\n"
        f"Skills available:\n{skills.list_skills()}\n"
        "Load a skill with load_skill before doing specialized work.\n\n"
        f"Repo memory index:\n{memory.index_prompt()}\n"
        "(Relevant memory content is injected automatically when present.)\n\n"
        "You can read repo metadata and issues freely; writing to the repo "
        "(labels, comments, closing) needs user approval.\n"
        "Read before you write. Never close an issue without explicit user approval.\n"
    )


def print_final_text(messages: list[dict]) -> None:
    last = messages[-1]["content"]
    if isinstance(last, list):
        for block in last:
            if getattr(block, "type", None) == "text":
                print(block.text)


# ── commands ──────────────────────────────────────────────
def cmd_ping(repo: str) -> int:
    try:
        r = GitHubClient().get_repo(repo)
        print(f"GitHub OK: {r['full_name']} ({r['stargazers_count']}★, "
              f"{r['open_issues_count']} open issues)")
        return 0
    except Exception as e:
        print(f"GitHub error: {e}")
        return 1


def build_loop_checked(repo: str, interactive: bool) -> AgentLoop:
    """build_loop, but report GitHub/LLM setup errors instead of tracebacking."""
    try:
        return build_loop(repo, interactive)
    except Exception as e:
        print(f"Setup error: {e}")
        sys.exit(1)


def cmd_triage(repo: str, limit: int, interactive: bool) -> int:
    task = (f"Triage the open issues in {repo} (up to {limit}). "
            f"First load the triage skill, then follow it exactly.")
    loop = build_loop_checked(repo, interactive)
    messages = loop.run([{"role": "user", "content": task}])
    print_final_text(messages)
    return 0


def cmd_interactive(repo: str, interactive: bool) -> None:
    loop = build_loop_checked(repo, interactive)
    history = []
    while True:
        try:
            query = input("\033[36mgha >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        loop.run(history)
        print_final_text(history)
        print()


# ── CLI ───────────────────────────────────────────────────
def main() -> int:
    p = argparse.ArgumentParser(description="gh-assistant - GitHub repo agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    ping = sub.add_parser("ping", help="check GitHub connectivity")
    ping.add_argument("repo")

    triage = sub.add_parser("triage", help="agent triages open issues")
    triage.add_argument("repo")
    triage.add_argument("--limit", type=int, default=10)
    triage.add_argument("--non-interactive", action="store_true",
                        help="deny any write op without asking")

    repl = sub.add_parser("interactive", help="REPL over the agent loop")
    repl.add_argument("repo", nargs="?", default=os.getenv("GHA_REPO", ""))
    repl.add_argument("--non-interactive", action="store_true")

    args = p.parse_args()

    if args.cmd == "ping":
        return cmd_ping(args.repo)
    if args.cmd == "triage":
        interactive = not args.non_interactive
        return cmd_triage(args.repo, args.limit, interactive)
    if args.cmd == "interactive":
        if not args.repo:
            print("interactive needs a repo (or set GHA_REPO in .env)")
            return 2
        interactive = not args.non_interactive
        cmd_interactive(args.repo, interactive)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
