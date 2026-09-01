#!/usr/bin/env python3
"""
context/compact.py — s08: four-layer compaction pipeline.

Cheap first, expensive later. Before each LLM call the loop runs:

    L3 tool_result_budget → persist oversized tool results to disk
    L1 snip_compact       → drop the middle of a long conversation
    L2 micro_compact      → replace old tool results with a placeholder
    L4 compact_history    → LLM summary when still over the threshold (1 API)

On prompt_too_long errors the loop calls reactive_compact (emergency: keep the
recent tail, summarize the head).

Order matters: budget must run before micro, or large results get placeholder'd
before their content is persisted (s08: "顺序不能换").
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

MAX_MESSAGES = 50
KEEP_HEAD = 3
KEEP_RECENT_TOOL_RESULTS = 3
TOOL_RESULT_BUDGET = 50_000        # chars; bigger results go to disk
TOKEN_THRESHOLD = 80_000           # char estimate (~/4 ≈ tokens)
MAX_COMPACT_FAILURES = 3           # circuit breaker


class Compactor:
    """Plugged into AgentLoop(compactor=...). before_send + reactive."""

    def __init__(self, *, client=None, model: str = "",
                 persist_dir: Path | None = None,
                 transcript_dir: Path | None = None,
                 threshold_chars: int = TOKEN_THRESHOLD,
                 max_messages: int = MAX_MESSAGES,
                 tool_result_budget: int = TOOL_RESULT_BUDGET,
                 enabled: bool = True):
        self.client = client
        self.model = model
        self.persist_dir = Path(persist_dir) if persist_dir else Path(".task_outputs/tool-results")
        self.transcript_dir = Path(transcript_dir) if transcript_dir else Path(".transcripts")
        self.threshold_chars = threshold_chars
        self.max_messages = max_messages
        self.tool_result_budget = tool_result_budget
        self.enabled = enabled
        self._failures = 0

    def before_send(self, messages: list[dict]) -> list[dict]:
        """Cheap preprocessors (0 API), then LLM compact only if still over."""
        if not self.enabled:
            return messages
        messages[:] = tool_result_budget(messages, max_bytes=self.tool_result_budget,
                                         output_dir=self.persist_dir)
        messages[:] = snip_compact(messages, max_messages=self.max_messages)
        messages[:] = micro_compact(messages)
        if estimate_chars(messages) > self.threshold_chars:
            self._compact_history(messages)
        return messages

    def reactive(self, messages: list[dict]) -> list[dict]:
        """Emergency path for prompt_too_long: keep the tail, summarize the head."""
        messages[:] = reactive_compact(messages, self.client, self.model,
                                       self.transcript_dir)
        return messages

    def _compact_history(self, messages: list[dict]) -> None:
        if self._failures >= MAX_COMPACT_FAILURES:
            return  # circuit breaker: stop burning API calls
        try:
            transcript = _write_transcript(messages, self.transcript_dir)
            summary = _summarize(messages, self.client, self.model)
        except Exception:
            self._failures += 1
            print(f"\033[90m[compact: summary failed ({self._failures}/{MAX_COMPACT_FAILURES})]\033[0m")
            return
        self._failures = 0
        print(f"\033[90m[compact: history summarized -> {transcript.name}]\033[0m")
        messages[:] = [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]


# ── L3: persist oversized tool results ─────────────────────
def tool_result_budget(messages: list[dict], max_bytes: int = TOOL_RESULT_BUDGET,
                       output_dir=None) -> list[dict]:
    last = messages[-1]
    c = last.get("content")
    if not isinstance(c, list):
        return messages
    blocks = [(i, b) for i, b in enumerate(c) if _block_type(b) == "tool_result"]
    total = sum(len(str(_block_content(b))) for _, b in blocks)
    if total <= max_bytes:
        return messages
    ranked = sorted(blocks, key=lambda p: len(str(_block_content(p[1]))), reverse=True)
    for _, b in ranked:
        if total <= max_bytes:
            break
        old = str(_block_content(b))
        path = _persist(old, b.get("tool_use_id", "result"), output_dir)
        preview = old[:2000]
        b["content"] = f'<persisted-output path="{path}">\n{preview}'
        total -= len(old) - len(preview)
    return messages


# ── L1: trim the middle of a long conversation ─────────────
def snip_compact(messages: list[dict], max_messages: int = MAX_MESSAGES) -> list[dict]:
    if len(messages) <= max_messages:
        return messages
    head_end = KEEP_HEAD
    tail_start = len(messages) - (max_messages - KEEP_HEAD)
    # Never split an assistant(tool_use) from its user(tool_result).
    if head_end > 0 and _has_tool_use(messages[head_end - 1]):
        while head_end < len(messages) and _is_tool_result_msg(messages[head_end]):
            head_end += 1
    if (tail_start > 0 and tail_start < len(messages)
            and _is_tool_result_msg(messages[tail_start])
            and _has_tool_use(messages[tail_start - 1])):
        tail_start -= 1
    snipped = tail_start - head_end
    placeholder = {"role": "user",
                   "content": f"[snipped {snipped} messages from conversation middle]"}
    return messages[:head_end] + [placeholder] + messages[tail_start:]


# ── L2: placeholder old tool results ───────────────────────
def micro_compact(messages: list[dict],
                  keep_recent: int = KEEP_RECENT_TOOL_RESULTS) -> list[dict]:
    results = [b for msg in messages
               if isinstance(msg.get("content"), list)
               for b in msg["content"] if _block_type(b) == "tool_result"]
    if len(results) <= keep_recent:
        return messages
    old_ids = {id(b) for b in results[:-keep_recent]}
    for msg in messages:
        c = msg.get("content")
        if isinstance(c, list):
            for b in c:
                if id(b) in old_ids and len(str(_block_content(b))) > 120:
                    b["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    return messages


# ── L4 / reactive: LLM summary ─────────────────────────────
def _summarize(messages: list[dict], client, model: str) -> str:
    prompt = (
        "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n"
        "Analyze the conversation, then produce a summary that preserves: "
        "current goal and remaining work, key findings and decisions, issues "
        "touched and their states, user constraints/preferences, open questions. "
        "First reason in <analysis>...</analysis>, then put the final summary "
        "inside <summary>...</summary>.\n\n"
        f"Conversation:\n{_render_messages(messages)[-40_000:]}"
    )
    resp = client.messages.create(model=model,
                                  messages=[{"role": "user", "content": prompt}],
                                  max_tokens=2000)
    text = _text_of(resp)
    m = re.search(r"<summary>(.*?)</summary>", text, re.S)
    return m.group(1).strip() if m else text.strip()


def reactive_compact(messages: list[dict], client, model: str,
                     transcript_dir=None) -> list[dict]:
    _write_transcript(messages, transcript_dir)
    tail_start = max(0, len(messages) - 5)
    if (tail_start > 0 and tail_start < len(messages)
            and _is_tool_result_msg(messages[tail_start])
            and _has_tool_use(messages[tail_start - 1])):
        tail_start -= 1
    summary = _summarize(messages[:tail_start], client, model)
    return [{"role": "user", "content": f"[Reactive compact]\n\n{summary}"},
            *messages[tail_start:]]


# ── persistence / estimation ───────────────────────────────
def _persist(content: str, tool_use_id: str, output_dir) -> str:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    name = f"{tool_use_id}_{int(time.time() * 1000)}.txt"
    (out / name).write_text(content, encoding="utf-8")
    return str(out / name)


def _write_transcript(messages: list[dict], transcript_dir):
    out = Path(transcript_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"gha-{int(time.time() * 1000)}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for m in messages:
            f.write(json.dumps(m, default=str) + "\n")
    return path


def estimate_chars(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
        elif isinstance(c, list):
            for b in c:
                t = _block_type(b)
                if t == "text":
                    total += len(getattr(b, "text", ""))
                elif t == "thinking":
                    total += len(getattr(b, "thinking", ""))
                else:
                    total += len(str(_block_content(b)))
                total += 60  # per-block overhead
    return total


# ── block/message introspection ────────────────────────────
def _block_type(b):
    return b.get("type") if isinstance(b, dict) else getattr(b, "type", None)


def _block_content(b):
    if isinstance(b, dict):
        return b.get("content", "")
    return getattr(b, "content", "")


def _has_tool_use(m: dict) -> bool:
    c = m.get("content")
    return isinstance(c, list) and any(_block_type(b) == "tool_use" for b in c)


def _is_tool_result_msg(m: dict) -> bool:
    c = m.get("content")
    return isinstance(c, list) and any(_block_type(b) == "tool_result" for b in c)


def _text_of(response) -> str:
    out = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            out.append(block.text)
    return "\n".join(out)


def _render_messages(messages: list[dict]) -> str:
    def val(b, key, default=""):
        return b.get(key, default) if isinstance(b, dict) else getattr(b, key, default)
    lines = []
    for m in messages:
        role = m.get("role")
        c = m.get("content")
        if isinstance(c, str):
            lines.append(f"[{role}] {c}")
        elif isinstance(c, list):
            for b in c:
                t = _block_type(b)
                if t == "tool_use":
                    lines.append(f"[tool_use] {val(b, 'name')}({_short(str(val(b, 'input')))})")
                elif t == "tool_result":
                    lines.append(f"[tool_result] {_short(str(_block_content(b)))}")
                elif t == "text":
                    lines.append(f"[assistant] {val(b, 'text')}")
                elif t == "thinking":
                    lines.append(f"[thinking] {_short(val(b, 'thinking'))}")
    return "\n".join(lines)


def _short(s: str, limit: int = 300) -> str:
    return s[:limit] + "..." if len(s) > limit else s
