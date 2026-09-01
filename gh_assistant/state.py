"""SQLite-backed run, event, checkpoint, task, and approval storage."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from gh_assistant.contracts import Message, RunPhase, RunStatus, messages_from_json, messages_to_json, new_id


SECRET_PATTERNS = [
    re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~-]+"),
    re.compile(r"\b(?:sk-ant-|sk-|gh[pousr]_)[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)(api[_-]?key|token|authorization|password)(\s*[:=]\s*)[^\s,;]+"),
]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def redact_text(value: str) -> str:
    redacted = value
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 2:
            redacted = pattern.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if _is_secret_key(str(key)):
                out[key] = "[REDACTED]"
            else:
                out[key] = redact_value(item)
        return out
    return value


def _is_secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    if normalized in {"token", "password", "secret", "api_key", "authorization"}:
        return True
    return normalized.endswith(("_token", "_password", "_secret", "_api_key"))


class StateStore:
    def __init__(self, state_dir: Path | str):
        self.root = Path(state_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.runs_dir = self.root / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "state.db"
        self._migrate()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def _migrate(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    repo TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    repo_path TEXT NOT NULL,
                    worktree_path TEXT NOT NULL DEFAULT '',
                    branch TEXT NOT NULL DEFAULT '',
                    base_ref TEXT NOT NULL DEFAULT '',
                    base_sha TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    status TEXT NOT NULL,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    correlation_id TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_run_seq ON events(run_id, seq);
                CREATE TABLE IF NOT EXISTS checkpoints (
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    actor TEXT NOT NULL,
                    turn INTEGER NOT NULL,
                    messages_json TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, actor)
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    subject_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    decided_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_approvals_run ON approvals(run_id, status);
                CREATE TABLE IF NOT EXISTS tool_calls (
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    call_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, call_id)
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    repo TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    source_hash TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(repo, name)
                );
                """
            )

    def run_dir(self, run_id: str) -> Path:
        path = self.runs_dir / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def create_run(
        self,
        *,
        repo: str,
        issue_number: int,
        repo_path: Path,
        provider: str,
        model: str,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        run_id = new_id("run_")
        now = utc_now()
        with self.connect() as db:
            db.execute(
                """INSERT INTO runs
                (id, repo, issue_number, repo_path, provider, model, phase, status,
                 config_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    repo,
                    issue_number,
                    str(Path(repo_path).resolve()),
                    provider,
                    model,
                    RunPhase.INTAKE.value,
                    RunStatus.CREATED.value,
                    json.dumps(config, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        self.run_dir(run_id)
        self.append_event(run_id, "run_created", "harness", {"repo": repo, "issue_number": issue_number})
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Run not found: {run_id}")
        return _decode_run(row)

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_decode_run(row) for row in rows]

    def update_run(self, run_id: str, **changes: Any) -> dict[str, Any]:
        allowed = {
            "worktree_path", "branch", "base_ref", "base_sha", "phase", "status",
            "result_json", "config_json",
        }
        invalid = set(changes) - allowed
        if invalid:
            raise ValueError(f"Invalid run fields: {sorted(invalid)}")
        if not changes:
            return self.get_run(run_id)
        values = dict(changes)
        for key in ("result_json", "config_json"):
            if key in values and not isinstance(values[key], str):
                values[key] = json.dumps(values[key], ensure_ascii=False)
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self.connect() as db:
            db.execute(
                f"UPDATE runs SET {assignments} WHERE id = ?",
                (*values.values(), run_id),
            )
        return self.get_run(run_id)

    def transition(self, run_id: str, phase: RunPhase, status: RunStatus = RunStatus.RUNNING) -> dict[str, Any]:
        before = self.get_run(run_id)
        run = self.update_run(run_id, phase=phase.value, status=status.value)
        self.append_event(
            run_id,
            "phase_changed",
            "harness",
            {"from": before["phase"], "to": phase.value, "status": status.value},
        )
        return run

    def append_event(
        self,
        run_id: str,
        event_type: str,
        actor: str,
        payload: dict[str, Any],
        correlation_id: str = "",
    ) -> int:
        safe_payload = redact_value(payload)
        with self.connect() as db:
            cur = db.execute(
                """INSERT INTO events
                (run_id, event_type, actor, correlation_id, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    event_type,
                    actor,
                    correlation_id,
                    json.dumps(safe_payload, ensure_ascii=False, default=str),
                    utc_now(),
                ),
            )
            return int(cur.lastrowid)

    def events(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
            ).fetchall()
        return [
            {
                "seq": row["seq"],
                "run_id": row["run_id"],
                "event_type": row["event_type"],
                "actor": row["actor"],
                "correlation_id": row["correlation_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def save_checkpoint(
        self,
        run_id: str,
        turn: int,
        messages: list[Message],
        state: dict[str, Any],
        *,
        actor: str = "main",
    ) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO checkpoints (run_id, actor, turn, messages_json, state_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, actor) DO UPDATE SET
                    turn=excluded.turn,
                    messages_json=excluded.messages_json,
                    state_json=excluded.state_json,
                    updated_at=excluded.updated_at""",
                (
                    run_id,
                    actor,
                    turn,
                    messages_to_json(messages),
                    json.dumps(state, ensure_ascii=False, default=str),
                    utc_now(),
                ),
            )

    def load_checkpoint(self, run_id: str, *, actor: str = "main") -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM checkpoints WHERE run_id = ? AND actor = ?",
                (run_id, actor),
            ).fetchone()
        if row is None:
            return None
        return {
            "turn": row["turn"],
            "messages": messages_from_json(row["messages_json"]),
            "state": json.loads(row["state_json"]),
            "updated_at": row["updated_at"],
        }

    def delete_checkpoint(self, run_id: str, *, actor: str) -> None:
        with self.connect() as db:
            db.execute(
                "DELETE FROM checkpoints WHERE run_id = ? AND actor = ?",
                (run_id, actor),
            )

    def replace_tasks(self, run_id: str, descriptions: list[str]) -> list[dict[str, Any]]:
        with self.connect() as db:
            db.execute("DELETE FROM tasks WHERE run_id = ?", (run_id,))
            for position, description in enumerate(descriptions):
                task_id = f"{run_id}_task_{position + 1}"
                db.execute(
                    "INSERT INTO tasks (id, run_id, position, description, status) VALUES (?, ?, ?, ?, ?)",
                    (task_id, run_id, position, description, "pending"),
                )
        return self.tasks(run_id)

    def tasks(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM tasks WHERE run_id = ? ORDER BY position", (run_id,)
            ).fetchall()
        return [
            {
                **dict(row),
                "evidence": json.loads(row["evidence_json"]),
            }
            for row in rows
        ]

    def create_approval(
        self,
        run_id: str,
        action: str,
        subject_hash: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        with self.connect() as db:
            existing = db.execute(
                """SELECT * FROM approvals
                WHERE run_id = ? AND action = ? AND subject_hash = ? AND status = 'pending'
                ORDER BY created_at DESC LIMIT 1""",
                (run_id, action, subject_hash),
            ).fetchone()
            if existing is not None:
                return _decode_approval(existing)
            approval_id = new_id("approval_")
            now = utc_now()
            db.execute(
                """INSERT INTO approvals
                (id, run_id, action, subject_hash, payload_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                (
                    approval_id,
                    run_id,
                    action,
                    subject_hash,
                    json.dumps(redact_value(payload), ensure_ascii=False, default=str),
                    now,
                ),
            )
        self.append_event(run_id, "approval_requested", "harness", {"approval_id": approval_id, "action": action, "subject_hash": subject_hash})
        return self.get_approval(approval_id)

    def get_approval(self, approval_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            raise KeyError(f"Approval not found: {approval_id}")
        return _decode_approval(row)

    def list_approvals(self, *, run_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if run_id:
            clauses.append("run_id = ?")
            values.append(run_id)
        if status:
            clauses.append("status = ?")
            values.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM approvals{where} ORDER BY created_at DESC", values
            ).fetchall()
        return [_decode_approval(row) for row in rows]

    def decide_approval(self, approval_id: str, decision: str) -> dict[str, Any]:
        if decision not in {"approved", "denied"}:
            raise ValueError("Approval decision must be approved or denied")
        approval = self.get_approval(approval_id)
        if approval["status"] != "pending":
            return approval
        with self.connect() as db:
            db.execute(
                "UPDATE approvals SET status = ?, decided_at = ? WHERE id = ?",
                (decision, utc_now(), approval_id),
            )
        updated = self.get_approval(approval_id)
        self.append_event(updated["run_id"], "approval_decided", "user", {"approval_id": approval_id, "decision": decision})
        return updated

    def begin_tool_call(self, run_id: str, call_id: str, tool_name: str) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO tool_calls (run_id, call_id, tool_name, status, updated_at)
                VALUES (?, ?, ?, 'started', ?)
                ON CONFLICT(run_id, call_id) DO NOTHING""",
                (run_id, call_id, tool_name, utc_now()),
            )

    def finish_tool_call(self, run_id: str, call_id: str, result: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                """UPDATE tool_calls SET status='completed', result_json=?, updated_at=?
                WHERE run_id=? AND call_id=?""",
                (json.dumps(redact_value(result), ensure_ascii=False), utc_now(), run_id, call_id),
            )

    def completed_tool_call(self, run_id: str, call_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT result_json FROM tool_calls WHERE run_id=? AND call_id=? AND status='completed'",
                (run_id, call_id),
            ).fetchone()
        return json.loads(row["result_json"]) if row else None


def _decode_run(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["config"] = json.loads(value.pop("config_json") or "{}")
    value["result"] = json.loads(value.pop("result_json") or "{}")
    return value


def _decode_approval(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["payload"] = json.loads(value.pop("payload_json") or "{}")
    return value
