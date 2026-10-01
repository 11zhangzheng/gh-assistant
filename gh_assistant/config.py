"""Configuration loading with a strict trust boundary."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def load_dotenv(path: Path | None = None, *, override: bool = False) -> None:
    """Load a small dotenv subset without making dotenv a startup dependency."""
    target = path or Path(".env")
    if not target.exists():
        return
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and (override or key not in os.environ):
            os.environ[key] = value


@dataclass(slots=True)
class BudgetConfig:
    max_main_turns: int = 30
    max_tool_calls: int = 120
    max_verification_repairs: int = 2
    max_review_repairs: int = 1
    max_tokens_per_call: int = 8_000
    max_wall_seconds: int = 1_800


@dataclass(slots=True)
class Settings:
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-6"
    fallback_model: str = ""
    anthropic_api_key: str = ""
    anthropic_base_url: str = ""
    openai_api_key: str = ""
    openai_base_url: str = ""
    github_token: str = ""
    state_dir: Path = Path(".gha")
    executor: str = "auto"
    docker_image: str = "gh-assistant-python:3.12"
    interactive: bool = True
    allow_local_verified: bool = True
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None
    budget: BudgetConfig = field(default_factory=BudgetConfig)

    @classmethod
    def from_env(cls, *, state_dir: str | Path | None = None) -> "Settings":
        load_dotenv(override=False)
        provider = os.getenv("GHA_PROVIDER", "anthropic").lower()
        model = os.getenv("GHA_MODEL") or os.getenv("MODEL_ID") or "claude-sonnet-4-6"
        return cls(
            provider=provider,
            model=model,
            fallback_model=os.getenv("GHA_FALLBACK_MODEL", ""),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            anthropic_base_url=os.getenv("ANTHROPIC_BASE_URL", ""),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_base_url=os.getenv("OPENAI_BASE_URL", ""),
            github_token=os.getenv("GITHUB_TOKEN", ""),
            state_dir=Path(state_dir or os.getenv("GHA_STATE_DIR", ".gha")),
            executor=os.getenv("GHA_EXECUTOR", "auto").lower(),
            docker_image=os.getenv("GHA_DOCKER_IMAGE", "gh-assistant-python:3.12"),
            allow_local_verified=os.getenv("GHA_ALLOW_LOCAL_VERIFIED", "true").lower() in {"1", "true", "yes"},
            input_cost_per_million=_optional_float(os.getenv("GHA_INPUT_COST_PER_MILLION")),
            output_cost_per_million=_optional_float(os.getenv("GHA_OUTPUT_COST_PER_MILLION")),
        )

    def validate_provider(self) -> list[str]:
        errors = []
        if self.provider not in {"anthropic", "openai", "scripted"}:
            errors.append(f"Unsupported provider: {self.provider}")
        if self.provider == "anthropic" and not self.anthropic_api_key:
            errors.append("ANTHROPIC_API_KEY is not configured")
        if self.provider == "openai" and not self.openai_api_key:
            errors.append("OPENAI_API_KEY is not configured")
        if not self.model:
            errors.append("Model ID is empty")
        return errors


@dataclass(slots=True)
class ProjectConfig:
    """Untrusted repo configuration. It can narrow behavior, never widen policy."""

    version: int = 1
    verify: list[list[str]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, repo_path: Path) -> "ProjectConfig":
        candidates = [repo_path / "gh-assistant.yaml", repo_path / ".gha.yaml"]
        path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if path is None:
            return cls(verify=detect_verification(repo_path))
        try:
            import yaml

            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise ValueError(f"Invalid project config {path}: {exc}") from exc
        allowed = {"version", "verify", "skills", "include", "exclude"}
        ignored = sorted(set(raw) - allowed)
        if ignored:
            raise ValueError(
                "Repo config contains privileged/unknown keys: " + ", ".join(ignored)
            )
        verify = _normalize_commands(raw.get("verify") or detect_verification(repo_path))
        return cls(
            version=int(raw.get("version", 1)),
            verify=verify,
            skills=[str(item) for item in raw.get("skills", [])],
            include=[str(item) for item in raw.get("include", [])],
            exclude=[str(item) for item in raw.get("exclude", [])],
        )


def detect_verification(repo_path: Path) -> list[list[str]]:
    if (repo_path / "pyproject.toml").exists() or (repo_path / "pytest.ini").exists():
        return [["python", "-m", "pytest", "-q"]]
    if (repo_path / "requirements.txt").exists() and (repo_path / "tests").exists():
        return [["python", "-m", "pytest", "-q"]]
    return []


def _normalize_commands(value: Any) -> list[list[str]]:
    if not isinstance(value, list):
        raise ValueError("verify must be a list of argv arrays")
    commands: list[list[str]] = []
    for item in value:
        if not isinstance(item, list) or not item or not all(isinstance(arg, str) for arg in item):
            raise ValueError("each verify command must be a non-empty argv array")
        commands.append(item)
    return commands


def _optional_float(value: str | None) -> float | None:
    return float(value) if value not in (None, "") else None
