from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "bug.py").write_text(
        "def add_one(value: int) -> int:\n    return value\n", encoding="utf-8"
    )
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_bug.py").write_text(
        "from bug import add_one\n\n"
        "def test_add_one():\n"
        "    assert add_one(1) == 2\n",
        encoding="utf-8",
    )
    (repo / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8"
    )
    python = str(Path(sys.executable).resolve()).replace("\\", "\\\\")
    (repo / "gh-assistant.yaml").write_text(
        "version: 1\n"
        "verify:\n"
        f'  - ["{python}", "-c", "from bug import add_one; assert add_one(1) == 2"]\n',
        encoding="utf-8",
    )
    _run(repo, ["git", "init", "-b", "main"])
    _run(repo, ["git", "add", "-A"])
    _run(
        repo,
        [
            "git",
            "-c",
            "user.name=tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
    )
    return repo


def _run(cwd: Path, argv: list[str]) -> str:
    result = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout
