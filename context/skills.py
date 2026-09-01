#!/usr/bin/env python3
"""
context/skills.py — s07: skill loading (two levels).

    Level 1 (catalog, startup): harness scans skills/ for SKILL.md files and
        injects name + description into the system prompt (~100 tokens/skill,
        carried every turn).
    Level 2 (content, on demand): the agent calls load_skill(name) and the full
        SKILL.md arrives as a tool_result (~2000 tokens, only when needed).

The registry is built at startup and never reads arbitrary paths at runtime
(no path-traversal: load_skill looks up the registry by name only).

    from context.skills import SkillRegistry
    skills = SkillRegistry(Path("skills"))
    print(skills.list_skills())
    print(skills.load_skill("triage"))
"""
from __future__ import annotations

from pathlib import Path

import yaml


class SkillRegistry:
    """Scan a skills/ directory once; serve catalog + contents."""

    def __init__(self, skills_dir: Path):
        self.skills_dir = Path(skills_dir)
        self.registry: dict[str, dict] = {}
        self.scan()

    def scan(self) -> None:
        self.registry = {}
        if not self.skills_dir.exists():
            return
        for d in sorted(self.skills_dir.iterdir()):
            if not d.is_dir():
                continue
            manifest = d / "SKILL.md"
            if not manifest.exists():
                continue
            raw = manifest.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(raw)
            name = meta.get("name", d.name)
            desc = meta.get("description", body.split("\n")[0].lstrip("#").strip())
            self.registry[name] = {
                "name": name,
                "description": desc,
                "content": raw,
            }

    def list_skills(self) -> str:
        """Level 1: the catalog line, injected into the system prompt."""
        if not self.registry:
            return "(no skills)"
        return "\n".join(
            f"- **{s['name']}**: {s['description']}" for s in self.registry.values())

    def load_skill(self, name: str) -> str:
        """Level 2: full SKILL.md content via tool_result."""
        skill = self.registry.get(name)
        if not skill:
            return f"Skill not found: {name}"
        return skill["content"]


def _parse_frontmatter(raw: str) -> tuple[dict, str]:
    if not raw.startswith("---"):
        return {}, raw
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except Exception:
        meta = {}
    return meta, parts[2]
