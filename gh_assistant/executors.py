"""Docker-first and explicitly approved local command executors."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from gh_assistant.contracts import ToolExecution


MAX_OUTPUT_CHARS = 50_000
SAFE_ENV_KEYS = {
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "PYTHONIOENCODING",
}
SECRET_ENV_FRAGMENTS = {"TOKEN", "SECRET", "PASSWORD", "API_KEY", "AUTHORIZATION"}


class ExecutorUnavailable(RuntimeError):
    pass


class LocalExecutor:
    name = "local"

    def __init__(self, workspace: Path, *, approved: bool = False):
        self.workspace = workspace.resolve()
        self.approved = approved

    def run(
        self,
        argv: list[str],
        *,
        cwd: str = ".",
        timeout_seconds: int = 120,
        env: dict[str, str] | None = None,
    ) -> ToolExecution:
        if not self.approved:
            return ToolExecution.error("Local execution requires run-level approval")
        command, workdir, process_env = _prepare_command(
            self.workspace, argv, cwd, timeout_seconds, env
        )
        executable_name = Path(command[0]).name.lower()
        if executable_name in {"python", "python3", "python.exe", "python3.exe"} or re.fullmatch(
            r"python3\.\d+(?:\.exe)?", executable_name
        ):
            if command[0] in {"python", "python3"}:
                command[0] = sys.executable
            with tempfile.TemporaryDirectory(prefix="gha-pycache-") as cache_dir:
                process_env["PYTHONPYCACHEPREFIX"] = cache_dir
                process_env["PYTHONDONTWRITEBYTECODE"] = "1"
                return _run_process(command, workdir, process_env, timeout_seconds)
        return _run_process(command, workdir, process_env, timeout_seconds)


class DockerExecutor:
    name = "docker"

    def __init__(
        self,
        workspace: Path,
        *,
        image: str = "gh-assistant-python:3.12",
        docker_binary: str = "docker",
    ):
        self.workspace = workspace.resolve()
        self.image = image
        self.docker_binary = docker_binary

    @classmethod
    def available(cls, docker_binary: str = "docker") -> tuple[bool, str]:
        if not shutil.which(docker_binary):
            return False, "Docker CLI is not installed"
        try:
            result = subprocess.run(
                [docker_binary, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        if result.returncode != 0:
            return False, (result.stderr or result.stdout or "Docker daemon unavailable").strip()
        return True, result.stdout.strip()

    def image_available(self) -> bool:
        result = subprocess.run(
            [self.docker_binary, "image", "inspect", self.image],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return result.returncode == 0

    def run(
        self,
        argv: list[str],
        *,
        cwd: str = ".",
        timeout_seconds: int = 120,
        env: dict[str, str] | None = None,
    ) -> ToolExecution:
        command, workdir, safe_env = _prepare_command(
            self.workspace, argv, cwd, timeout_seconds, env
        )
        available, reason = self.available(self.docker_binary)
        if not available:
            return ToolExecution.error(f"Docker unavailable: {reason}")
        if not self.image_available():
            return ToolExecution.error(
                f"Docker image {self.image!r} is not present. "
                "Build it with `docker build -t gh-assistant-python:3.12 docker`."
            )
        relative = workdir.relative_to(self.workspace).as_posix()
        container_cwd = "/workspace" if relative == "." else f"/workspace/{relative}"
        if command[0] in {"python", "python3"}:
            safe_env["PYTHONPYCACHEPREFIX"] = "/tmp/gha-pycache"
            safe_env["PYTHONDONTWRITEBYTECODE"] = "1"
        container_user = "65532:65532"
        if os.name != "nt" and hasattr(os, "getuid") and os.getuid() != 0:
            container_user = f"{os.getuid()}:{os.getgid()}"
        docker_command = [
            self.docker_binary,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
            "--memory",
            "2g",
            "--cpus",
            "2",
            "--user",
            container_user,
            "--env",
            "HOME=/tmp",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=256m",
            "--mount",
            f"type=bind,source={self.workspace},target=/workspace",
            "--workdir",
            container_cwd,
        ]
        for key, value in safe_env.items():
            if key not in SAFE_ENV_KEYS:
                docker_command.extend(["--env", f"{key}={value}"])
        docker_command.extend([self.image, *command])
        return _run_process(docker_command, self.workspace, _base_env(), timeout_seconds)


def choose_executor(
    workspace: Path,
    *,
    preference: str,
    docker_image: str,
    local_approved: bool,
):
    if preference not in {"auto", "docker", "local"}:
        raise ValueError(f"Unknown executor preference: {preference}")
    if preference == "local" and not local_approved:
        return None, "Local executor was explicitly selected but has not been approved"
    if preference in {"auto", "docker"}:
        available, reason = DockerExecutor.available()
        if available:
            return DockerExecutor(workspace, image=docker_image), None
        if preference == "docker":
            raise ExecutorUnavailable(reason)
        if not local_approved:
            return None, reason
    return LocalExecutor(workspace, approved=local_approved), None


def _prepare_command(
    workspace: Path,
    argv: list[str],
    cwd: str,
    timeout_seconds: int,
    env: dict[str, str] | None,
) -> tuple[list[str], Path, dict[str, str]]:
    if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) and arg for arg in argv):
        raise ValueError("argv must be a non-empty array of non-empty strings")
    if not 1 <= timeout_seconds <= 300:
        raise ValueError("timeout_seconds must be between 1 and 300")
    workdir = _safe_path(workspace, cwd)
    if not workdir.is_dir():
        raise ValueError(f"Command cwd is not a directory: {cwd}")
    process_env = _base_env()
    for key, value in (env or {}).items():
        upper = key.upper()
        if any(fragment in upper for fragment in SECRET_ENV_FRAGMENTS):
            raise ValueError(f"Secret-like environment variable is not allowed: {key}")
        if not key or "=" in key or not isinstance(value, str):
            raise ValueError(f"Invalid environment variable: {key!r}")
        process_env[key] = value
    return list(argv), workdir, process_env


def _base_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key.upper() in SAFE_ENV_KEYS}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _safe_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Path escapes workspace: {relative}") from exc
    return candidate


def _run_process(
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
) -> ToolExecution:
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return ToolExecution.error(
            f"Command timed out after {timeout_seconds}s",
            {"timeout_seconds": timeout_seconds, "stdout": _cap(exc.stdout or "")},
        )
    except OSError as exc:
        return ToolExecution.error(f"Command could not start: {exc}")
    duration_ms = int((time.monotonic() - started) * 1000)
    stdout = _cap(result.stdout)
    stderr = _cap(result.stderr)
    rendered = stdout
    if stderr:
        rendered += ("\n" if rendered else "") + f"[stderr]\n{stderr}"
    rendered = rendered or "(no output)"
    data = {
        "argv": command,
        "exit_code": result.returncode,
        "duration_ms": duration_ms,
        "stdout": stdout,
        "stderr": stderr,
    }
    if result.returncode == 0:
        return ToolExecution.ok(rendered, data)
    return ToolExecution.error(f"exit_code={result.returncode}\n{rendered}", data)


def _cap(value: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n[truncated {len(value) - limit} chars]"
