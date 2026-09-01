#!/usr/bin/env python3
"""
memory/memory.py — s09: file-based per-repo memory.

Storage:    one .md file per memory (YAML frontmatter) + a MEMORY.md index.
Loading:    path 1 = index injected into the system prompt (cheap, every turn);
            path 2 = LLM side-query picks relevant memories, contents injected
            into the current user turn (up to MAX_INJECT, with keyword fallback).
Writing:    extract() runs after each turn ends (stop_reason != "tool_use");
            the LLM turns the tail of the dialogue into new memory files.
Consolidate: once files pass a threshold, LLM dedupes/merges (s09 "Dream",
            simplified here from four-layer gating to a file-count trigger).

    store = MemoryStore(Path(".memory/owner_repo"), client=llm, model=MODEL)
    store.write("triage-probe-issues", "feedback", "Probe issues -> invalid", "...")
    prompt = store.relevant_prompt("triage the open issues")   # side-query
    n = store.extract(messages)                                # end of turn
"""
from __future__ import annotations

import json
import re
from pathlib import Path

MEMORY_TYPES = ("user", "feedback", "project", "reference")
CONSOLIDATE_THRESHOLD = 10
MAX_INJECT = 5


class MemoryStore:
    def __init__(self, root: Path, *, client=None, model: str = ""):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.client = client
        self.model = model

    # ── storage ─────────────────────────────────────────────
    def files(self) -> list[dict]:
        out = []
        for f in sorted(self.root.glob("*.md")):
            if f.name == "MEMORY.md":
                continue
            meta, _ = _parse_frontmatter(f.read_text(encoding="utf-8"))
            out.append({
                "path": f,
                "name": meta.get("name", f.stem),
                "description": meta.get("description", ""),
                "type": meta.get("type", "project"),
            })
        return out

    def write(self, name: str, mem_type: str = "project",
              description: str = "", body: str = "") -> Path:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "memory"
        path = self.root / f"{slug}.md"
        path.write_text(
            f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n\n{body}\n",
            encoding="utf-8")
        self._rebuild_index()
        return path

    def _rebuild_index(self) -> None:
        lines = [f"- [{m['name']}]({m['path'].name}) — {m['description']}"
                 for m in self.files()]
        (self.root / "MEMORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def index_prompt(self) -> str:
        files = self.files()
        if not files:
            return "(no memories yet)"
        return "\n".join(f"- {m['name']} — {m['description']}" for m in files)

    # ── loading: path 2 (LLM side-query) ────────────────────
    def select_relevant(self, recent_text: str, max_items: int = MAX_INJECT) -> list[str]:
        files = self.files()
        if not files or not self.client:
            return []
        catalog = "\n".join(f"{i}: {f['name']} — {f['description']}"
                            for i, f in enumerate(files))
        prompt = (
            "Select the most relevant memory indices for the current task. "
            "Be conservative; only select memories that clearly help. "
            "Return a JSON array of indices.\n\n"
            f"Current task:\n{recent_text[-1500:]}\n\nMemory catalog:\n{catalog}"
        )
        try:
            resp = self.client.messages.create(
                model=self.model, messages=[{"role": "user", "content": prompt}],
                max_tokens=200)
            arr = _extract_json_array(_text_of(resp))
            return [files[i]["path"].name for i in arr
                    if isinstance(i, int) and 0 <= i < len(files)]
        except Exception:
            return self._keyword_fallback(recent_text)

    def _keyword_fallback(self, text: str) -> list[str]:
        # Match significant tokens of the query against each memory's
        # name + description; most matches first.
        low = text.lower()
        tokens = {t for t in re.split(r"[^a-z0-9]+", low) if len(t) >= 3}
        scored = []
        for f in self.files():
            hay = (f["name"] + " " + f["description"]).lower()
            n = sum(1 for t in tokens if t in hay)
            if n:
                scored.append((n, f["path"].name))
        scored.sort(reverse=True)
        return [name for _, name in scored[:MAX_INJECT]]

    def load(self, names: list[str]) -> str:
        parts = []
        for n in names:
            p = self.root / n
            if p.exists():
                parts.append(p.read_text(encoding="utf-8"))
        return "\n\n".join(parts)

    def relevant_prompt(self, recent_text: str) -> str:
        """The memory block to inject into the current user turn."""
        return self.load(self.select_relevant(recent_text))

    # ── writing: extract at end of turn ─────────────────────
    def extract(self, messages: list[dict]) -> int:
        if not self.client:
            return 0
        dialogue = _render_recent(messages, last=10)
        if not dialogue.strip():
            return 0
        existing = "\n".join(f"- {m['name']}: {m['description']}"
                             for m in self.files()) or "(none)"
        prompt = (
            "You are a memory extractor for a GitHub repo maintenance agent. "
            "From the dialogue, extract durable repo conventions, decisions, and "
            "maintainer preferences that would help in future sessions (e.g. label "
            "conventions, how to handle junk issues, repo quirks, triage rules). "
            "Return a JSON array of {name, type, description, body}, where type is one "
            f"of {', '.join(MEMORY_TYPES)}. If nothing new or already covered, return [].\n\n"
            f"Existing memories:\n{existing}\n\nDialogue:\n{dialogue[:4000]}"
        )
        try:
            resp = self.client.messages.create(
                model=self.model, messages=[{"role": "user", "content": prompt}],
                max_tokens=800)
            mems = _extract_json_array(_text_of(resp))
        except Exception:
            return 0
        n = 0
        for m in mems:
            name, body = m.get("name"), m.get("body")
            if not name or not body:
                continue
            if any(f["name"] == name for f in self.files()):
                continue  # already covered — skip duplicates
            typ = m.get("type", "project")
            if typ not in MEMORY_TYPES:
                typ = "project"
            self.write(name, typ, m.get("description", ""), body)
            n += 1
        self._maybe_consolidate()
        return n

    # ── consolidation (s09 "Dream", simplified) ─────────────
    def _maybe_consolidate(self) -> None:
        if len(self.files()) >= CONSOLIDATE_THRESHOLD:
            self.consolidate()

    def consolidate(self) -> int:
        files = self.files()
        if not self.client or len(files) < CONSOLIDATE_THRESHOLD:
            return 0
        listing = "\n".join(
            f"--- {m['name']} ({m['type']}) ---\n"
            f"{m['path'].read_text(encoding='utf-8')}" for m in files)
        prompt = (
            "Consolidate these memories: merge duplicates, resolve conflicts "
            "(newer wins), drop obsolete ones. Return a JSON array of "
            "{name, type, description, body}. Keep every unique fact.\n\n" + listing
        )
        try:
            resp = self.client.messages.create(
                model=self.model, messages=[{"role": "user", "content": prompt}],
                max_tokens=2000)
            mems = _extract_json_array(_text_of(resp))
        except Exception:
            return 0
        for f in files:
            f["path"].unlink(missing_ok=True)
        n = 0
        for m in mems:
            if m.get("name") and m.get("body"):
                self.write(m["name"], m.get("type", "project"),
                           m.get("description", ""), m["body"])
                n += 1
        self._rebuild_index()
        return n


# ── helpers ────────────────────────────────────────────────
def _parse_frontmatter(raw: str):
    if not raw.startswith("---"):
        return {}, raw
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw
    try:
        import yaml
        meta = yaml.safe_load(parts[1]) or {}
    except Exception:
        meta = {}
    return meta, parts[2]


def _text_of(response) -> str:
    out = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            out.append(block.text)
    return "\n".join(out)


def _extract_json_array(text: str) -> list:
    """Pull the first balanced JSON array out of LLM text.

    Regex \[.*?\] fails when a body string contains a `]`; this scans with
    bracket balancing and skips brackets inside quoted strings (handling
    escapes), so it finds the array's true closing bracket.
    """
    if not text:
        return []
    start = text.find("[")
    if start < 0:
        return []
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    val = json.loads(text[start:i + 1])
                    return val if isinstance(val, list) else []
                except Exception:
                    return []
    return []


def _render_recent(messages: list[dict], last: int) -> str:
    def val(b, key, default=""):
        return b.get(key, default) if isinstance(b, dict) else getattr(b, key, default)
    lines = []
    for msg in messages[-last:]:
        role = msg.get("role")
        c = msg.get("content")
        if isinstance(c, str):
            lines.append(f"[{role}] {c[:600]}")
        elif isinstance(c, list):
            for b in c:
                btype = val(b, "type")
                if btype == "tool_use":
                    lines.append(f"[tool_use] {val(b, 'name')}")
                elif btype == "tool_result":
                    lines.append(f"[tool_result] {_short(str(val(b, 'content')))}")
                elif btype == "text":
                    lines.append(f"[assistant] {val(b, 'text')[:600]}")
    return "\n".join(lines)


def _short(s: str, limit: int = 300) -> str:
    return s[:limit] + "..." if len(s) > limit else s
