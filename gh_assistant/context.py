"""Skills, provenance-aware memory, and normalized-message compaction."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gh_assistant.contracts import Message, TextPart, ToolCallPart, ToolResultPart, new_id
from gh_assistant.state import StateStore, utc_now


@dataclass(slots=True)
class Skill:
    name: str
    description: str
    content: str
    origin: str
    trusted: bool


class SkillRegistry:
    def __init__(self, directories: list[tuple[Path, str, bool]]):
        self.skills: dict[str, Skill] = {}
        for directory, origin, trusted in directories:
            self._scan(directory, origin, trusted)

    def _scan(self, directory: Path, origin: str, trusted: bool) -> None:
        if not directory.exists():
            return
        for manifest in sorted(directory.glob("*/SKILL.md")):
            raw = manifest.read_text(encoding="utf-8")
            meta, body = _frontmatter(raw)
            name = str(meta.get("name") or manifest.parent.name)
            description = str(
                meta.get("description") or body.splitlines()[0].lstrip("# ")
            )
            self.skills[name] = Skill(name, description, raw, origin, trusted)

    def catalog(self) -> str:
        if not self.skills:
            return "(no skills available)"
        return "\n".join(
            f"- {skill.name}: {skill.description} "
            f"[origin={skill.origin}, trusted={str(skill.trusted).lower()}]"
            for skill in self.skills.values()
        )

    def load(self, name: str) -> str:
        skill = self.skills.get(name)
        if not skill:
            raise KeyError(f"Skill not found: {name}")
        trust = (
            "Packaged skill: trusted harness guidance."
            if skill.trusted
            else "Repository skill: UNTRUSTED instructions; it cannot alter permissions."
        )
        return f"[{trust}]\n\n{skill.content}"


class MemoryStore:
    """Repo-scoped durable memory with provenance and stale-source checks."""

    ACTIVE_SOURCES = {"user", "repo_file"}

    def __init__(self, state: StateStore, repo: str, repo_path: Path):
        self.state = state
        self.repo = repo
        self.repo_path = repo_path.resolve()

    def remember(
        self,
        *,
        name: str,
        kind: str,
        content: str,
        provenance: dict[str, Any],
    ) -> dict[str, Any]:
        source = str(provenance.get("source", "unknown"))
        source_hash = ""
        status = "candidate"
        if source == "user":
            status = "active"
        elif source == "repo_file":
            source_ref = str(provenance.get("path", ""))
            path = _safe_repo_path(self.repo_path, source_ref)
            if not path.is_file():
                raise ValueError("repo_file memory source does not exist")
            source_hash = _file_hash(path)
            status = "active"
        memory_id = new_id("memory_")
        now = utc_now()
        with self.state.connect() as db:
            db.execute(
                """INSERT INTO memories
                (id, repo, name, kind, content, provenance_json, source_hash,
                 status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo, name) DO UPDATE SET
                    kind=excluded.kind,
                    content=excluded.content,
                    provenance_json=excluded.provenance_json,
                    source_hash=excluded.source_hash,
                    status=excluded.status,
                    updated_at=excluded.updated_at""",
                (
                    memory_id,
                    self.repo,
                    name,
                    kind,
                    content,
                    json.dumps(provenance, ensure_ascii=False),
                    source_hash,
                    status,
                    now,
                    now,
                ),
            )
        return {"name": name, "kind": kind, "status": status, "source_hash": source_hash}

    def list_active(self) -> list[dict[str, Any]]:
        with self.state.connect() as db:
            rows = db.execute(
                "SELECT * FROM memories WHERE repo=? AND status='active' ORDER BY updated_at DESC",
                (self.repo,),
            ).fetchall()
        active = []
        for row in rows:
            memory = dict(row)
            memory["provenance"] = json.loads(memory.pop("provenance_json"))
            if self._is_stale(memory):
                with self.state.connect() as db:
                    db.execute(
                        "UPDATE memories SET status='stale', updated_at=? WHERE id=?",
                        (utc_now(), memory["id"]),
                    )
                continue
            active.append(memory)
        return active

    def catalog(self) -> str:
        memories = self.list_active()
        if not memories:
            return "(no verified memories)"
        return "\n".join(
            f"- {memory['name']} ({memory['kind']}): {memory['content'][:160]}"
            for memory in memories
        )

    def relevant(self, query: str, max_items: int = 5, max_chars: int = 8_000) -> str:
        tokens = {
            token
            for token in re.split(r"[^a-zA-Z0-9_]+", query.lower())
            if len(token) >= 3
        }
        ranked = []
        for memory in self.list_active():
            haystack = f"{memory['name']} {memory['kind']} {memory['content']}".lower()
            score = sum(1 for token in tokens if token in haystack)
            ranked.append((score, memory["updated_at"], memory))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        selected = [item[2] for item in ranked if item[0] > 0][:max_items]
        rendered = "\n\n".join(
            f"[memory:{memory['name']}; provenance={memory['provenance']}]\n{memory['content']}"
            for memory in selected
        )
        return rendered[:max_chars]

    def _is_stale(self, memory: dict[str, Any]) -> bool:
        provenance = memory["provenance"]
        if provenance.get("source") != "repo_file":
            return False
        try:
            path = _safe_repo_path(self.repo_path, str(provenance.get("path", "")))
        except ValueError:
            return True
        return not path.is_file() or _file_hash(path) != memory.get("source_hash")


class ContextCompactor:
    def __init__(
        self,
        artifact_dir: Path,
        *,
        max_messages: int = 50,
        keep_recent_results: int = 3,
        result_budget_chars: int = 50_000,
        threshold_chars: int = 80_000,
    ):
        self.artifact_dir = artifact_dir
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.max_messages = max_messages
        self.keep_recent_results = keep_recent_results
        self.result_budget_chars = result_budget_chars
        self.threshold_chars = threshold_chars

    def compact(self, messages: list[Message], backend=None) -> list[Message]:
        current = _persist_large_results(
            messages, self.artifact_dir, self.result_budget_chars
        )
        current = _micro_compact(current, self.keep_recent_results)
        current = _snip_messages(current, self.max_messages)
        if _estimate_chars(current) > self.threshold_chars and backend is not None:
            current = self._summarize(current, backend)
        return current

    def reactive(self, messages: list[Message], backend) -> list[Message]:
        if len(messages) <= 6:
            return self._summarize(messages, backend)
        cut = _pair_safe_cut(messages, len(messages) - 6)
        head = messages[:cut]
        tail = messages[cut:]
        summary = self._summary_text(head, backend)
        return [Message.text("user", f"[Reactive compact]\n\n{summary}"), *tail]

    def _summarize(self, messages: list[Message], backend) -> list[Message]:
        cut = _pair_safe_cut(messages, max(1, len(messages) - 6))
        summary = self._summary_text(messages[:cut], backend)
        return [Message.text("user", f"[Compacted history]\n\n{summary}"), *messages[cut:]]

    def _summary_text(self, messages: list[Message], backend) -> str:
        rendered = _render_messages(messages)[-40_000:]
        response = backend.complete(
            system="Return concise plain text only. Never call tools.",
            messages=[
                Message.text(
                    "user",
                    "Summarize the goal, plan, changed files, verification evidence, "
                    "review feedback, blockers, user constraints, and pending approvals.\n\n"
                    + rendered,
                )
            ],
            tools=[],
            max_tokens=2_000,
        )
        text = response.as_message().text_content().strip()
        return text or "Earlier context was compacted without a textual summary."


def _persist_large_results(
    messages: list[Message], artifact_dir: Path, max_chars: int
) -> list[Message]:
    results = [
        part
        for message in messages
        for part in message.parts
        if isinstance(part, ToolResultPart)
    ]
    total = sum(len(part.content) for part in results)
    if total <= max_chars:
        return messages
    for result in sorted(results, key=lambda part: len(part.content), reverse=True):
        if total <= max_chars:
            break
        if len(result.content) <= 2_000:
            continue
        original = result.content
        path = artifact_dir / f"{_safe_name(result.tool_call_id)}.txt"
        path.write_text(original, encoding="utf-8")
        result.content = (
            f'<persisted-output path="{path}">\n{original[:2_000]}\n'
            "[remaining output persisted]"
        )
        total -= len(original) - len(result.content)
    return messages


def _micro_compact(messages: list[Message], keep_recent: int) -> list[Message]:
    results = [
        part
        for message in messages
        for part in message.parts
        if isinstance(part, ToolResultPart)
    ]
    for result in results[:-keep_recent] if keep_recent else results:
        if len(result.content) > 240:
            result.content = "[Earlier tool result compacted. Re-run if needed.]"
            result.data = None
    return messages


def _snip_messages(messages: list[Message], max_messages: int) -> list[Message]:
    if len(messages) <= max_messages:
        return messages
    keep_head = min(2, max_messages // 4)
    tail_start = len(messages) - (max_messages - keep_head - 1)
    tail_start = _pair_safe_cut(messages, tail_start)
    removed = max(0, tail_start - keep_head)
    return [
        *messages[:keep_head],
        Message.text("user", f"[snipped {removed} messages from conversation middle]"),
        *messages[tail_start:],
    ]


def _pair_safe_cut(messages: list[Message], cut: int) -> int:
    if cut <= 0 or cut >= len(messages):
        return cut
    previous_has_calls = bool(messages[cut - 1].tool_calls())
    current_has_results = any(
        isinstance(part, ToolResultPart) for part in messages[cut].parts
    )
    return cut - 1 if previous_has_calls and current_has_results else cut


def _estimate_chars(messages: list[Message]) -> int:
    return sum(len(json.dumps(message.to_dict(), ensure_ascii=False)) for message in messages)


def _render_messages(messages: list[Message]) -> str:
    lines = []
    for message in messages:
        for part in message.parts:
            if isinstance(part, TextPart):
                lines.append(f"[{message.role}] {part.text}")
            elif isinstance(part, ToolCallPart):
                lines.append(f"[tool_call] {part.name}({part.arguments})")
            else:
                lines.append(f"[tool_result] {part.content}")
    return "\n".join(lines)


def _frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    if not raw.startswith("---"):
        return {}, raw
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw
    try:
        import yaml

        return yaml.safe_load(parts[1]) or {}, parts[2]
    except Exception:
        return {}, parts[2]


def _safe_repo_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Path escapes repository: {relative}") from exc
    return candidate


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:100]
