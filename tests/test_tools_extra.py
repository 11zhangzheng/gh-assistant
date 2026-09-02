from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gh_assistant.context import SkillRegistry
from gh_assistant.contracts import ToolEffect, ToolExecution, ToolSpec
from gh_assistant.tools import (
    ToolRegistry,
    ToolValidationError,
    build_workspace_tools,
    object_schema,
    safe_path,
    validate_arguments,
)


class Executor:
    def __init__(self):
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return ToolExecution.ok("ran", {"exit_code": 0})


def _skills(tmp_path: Path) -> SkillRegistry:
    packaged = tmp_path / "packaged"
    packaged.mkdir()
    skill = packaged / "python-testing"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# Test skill", encoding="utf-8")
    return SkillRegistry([(packaged, "packaged", True)])


def test_registry_registration_subset_validation_and_handler_errors():
    registry = ToolRegistry()
    spec = ToolSpec(
        "echo", "echo", object_schema({"value": {"type": "string"}}, ("value",)), ToolEffect.READ
    )
    registry.register(spec, lambda value: ToolExecution.ok(value))
    assert registry.specs == [spec]
    assert registry.spec("missing") is None
    assert registry.execute("echo", {"value": "x"}).content == "x"
    assert registry.execute("unknown", {}).is_error
    assert registry.execute("echo", {}).is_error
    assert registry.subset({"echo"}).execute("echo", {"value": "y"}).content == "y"
    with pytest.raises(ValueError, match="already registered"):
        registry.register(spec, lambda: ToolExecution.ok("x"))
    with pytest.raises(ValueError, match="schema must be an object"):
        ToolRegistry().register(
            ToolSpec("bad", "bad", {"type": "array"}, ToolEffect.READ), lambda: ToolExecution.ok("x")
        )

    exploding = ToolRegistry()
    exploding.register(ToolSpec("boom", "", object_schema({}), ToolEffect.READ), lambda: 1 / 0)
    assert "ZeroDivisionError" in exploding.execute("boom", {}).content

    concise = ToolRegistry()
    concise.add(
        "echo",
        "echo",
        lambda value: ToolExecution.ok(value),
        properties={"value": {"type": "string"}},
        required=("value",),
    )
    assert concise.execute("echo", {"value": "short"}).content == "short"


def test_workspace_file_search_patch_command_git_and_skills(tmp_path: Path):
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "a.txt").write_text("first\nneedle\nneedle\n", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"\xff\xfe")
    executor = Executor()
    registry = build_workspace_tools(tmp_path, executor=executor, skills=_skills(tmp_path))
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        [
            "git", "-c", "user.name=tests", "-c", "user.email=tests@example.invalid",
            "commit", "-m", "fixture",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    assert "2:needle" in registry.execute("read_file", {"path": "a.txt", "start_line": 2, "end_line": 2}).content
    assert registry.execute("read_file", {"path": "missing"}).is_error
    assert registry.execute("read_file", {"path": "a.txt", "start_line": 3, "end_line": 2}).is_error
    assert registry.execute("read_file", {"path": "binary.bin"}).is_error
    assert "a.txt" in registry.execute("list_files", {"pattern": "**/*"}).content
    assert registry.execute("list_files", {"pattern": "../*"}).is_error
    assert registry.execute("search_text", {"query": ""}).is_error
    matches = registry.execute("search_text", {"query": "needle", "limit": 1})
    assert matches.data["count"] == 1

    assert registry.execute(
        "apply_patch", {"path": "missing.txt", "old_text": "x", "new_text": "y"}
    ).is_error
    assert registry.execute(
        "apply_patch", {"path": "a.txt", "old_text": "", "new_text": "x"}
    ).is_error
    assert registry.execute(
        "apply_patch", {"path": "a.txt", "old_text": "absent", "new_text": "x"}
    ).is_error
    assert registry.execute(
        "apply_patch", {"path": "a.txt", "old_text": "needle", "new_text": "x"}
    ).is_error
    changed = registry.execute(
        "apply_patch",
        {"path": "a.txt", "old_text": "needle", "new_text": "x", "replace_all": True},
    )
    assert not changed.is_error and (tmp_path / "a.txt").read_text().count("x") == 2
    created = registry.execute(
        "apply_patch", {"path": "new/created.txt", "old_text": "", "new_text": "new"}
    )
    assert not created.is_error and created.data["created"]

    ran = registry.execute("run_command", {"argv": ["python", "-V"], "cwd": "."})
    assert not ran.is_error and executor.calls
    assert "a.txt" in registry.execute("git_status", {}).content
    assert "needle" in registry.execute("git_diff", {}).content
    assert "Test skill" in registry.execute("load_skill", {"name": "python-testing"}).content
    assert registry.execute("load_skill", {"name": "missing"}).is_error


def test_safe_path_and_argument_schema_edges(tmp_path: Path):
    assert safe_path(tmp_path, "new.txt", allow_missing=True) == tmp_path / "new.txt"
    with pytest.raises(ValueError, match="non-empty relative"):
        safe_path(tmp_path, "")
    with pytest.raises(ValueError, match="escapes"):
        safe_path(tmp_path, "../outside", allow_missing=True)

    schema = {
        "type": "object",
        "properties": {
            "values": {"type": "array", "items": {"type": "integer"}, "minItems": 1},
            "mode": {"type": "string", "enum": ["a", "b"]},
            "ratio": {"type": "number", "minimum": 0, "maximum": 1},
            "flag": {"type": "boolean"},
        },
        "required": ["values"],
        "additionalProperties": False,
    }
    validate_arguments(schema, {"values": [1], "mode": "a", "ratio": 0.5, "flag": True})
    invalid = [
        None,
        {},
        {"values": []},
        {"values": [True]},
        {"values": [1], "extra": 1},
        {"values": [1], "mode": "c"},
        {"values": [1], "ratio": -1},
        {"values": [1], "ratio": 2},
        {"values": [1], "flag": 1},
    ]
    for value in invalid:
        with pytest.raises(ToolValidationError):
            validate_arguments(schema, value)

    additional = {"type": "object", "properties": {}, "additionalProperties": {"type": "string"}}
    validate_arguments(additional, {"key": "value"})
    with pytest.raises(ToolValidationError):
        validate_arguments(additional, {"key": 1})
