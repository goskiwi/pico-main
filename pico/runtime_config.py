"""Normalized configuration for one Pico runtime."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from typing import Any

from .workspace import normalize_relative_file

DEFAULT_SHELL_ENV_ALLOWLIST = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "PWD",
    "SHELL",
    "TERM",
    "TMPDIR",
    "TMP",
    "TEMP",
    "USER",
)
def _allowed_tools(value):
    if value is None:
        return None
    normalized = tuple(str(name).strip() for name in value)
    if not normalized or any(not name for name in normalized):
        raise ValueError("allowed_tools must be a non-empty sequence of tool names")
    return normalized


def _allowed_write_paths(value):
    if value is None:
        return None
    normalized = tuple(normalize_relative_file(path) for path in value)
    if len(set(normalized)) != len(normalized):
        raise ValueError("allowed_write_paths must be unique")
    return normalized


@dataclass(frozen=True, slots=True)
class PicoConfig:
    """Runtime policy, resource limits, and bounded tool surface."""

    approval_policy: str = "ask"
    max_tool_executions: int | None = None
    max_new_tokens: int = 1024
    read_only: bool = False
    shell_env_allowlist: tuple[str, ...] = DEFAULT_SHELL_ENV_ALLOWLIST
    secret_env_names: frozenset[str] = field(default_factory=frozenset)
    allowed_tools: tuple[str, ...] | None = None
    run_timeout_seconds: int = 600
    provider_context_limit_tokens: int = 272000
    compaction_reserve_tokens: int = 16384
    compaction_keep_recent_tokens: int = 20000
    sandbox_image: str = "pico/sandbox:latest"
    verification_command: str | None = None
    allowed_write_paths: tuple[str, ...] | None = None
    subagent_max_workers: int = 3

    @classmethod
    def build(cls, config: PicoConfig | None = None, **overrides: Any) -> PicoConfig:
        known = {item.name for item in fields(cls)}
        unknown = sorted(set(overrides) - known)
        if unknown:
            raise TypeError(f"unknown Pico configuration: {', '.join(unknown)}")
        candidate = replace(config, **overrides) if config is not None else cls(**overrides)
        return candidate.normalized()

    def normalized(self) -> PicoConfig:
        if self.approval_policy not in {"ask", "auto", "never"}:
            raise ValueError("approval_policy must be ask, auto, or never")
        max_new_tokens = int(self.max_new_tokens)
        if max_new_tokens < 1:
            raise ValueError("max_new_tokens must be positive")
        max_tool_executions = (
            None
            if self.max_tool_executions is None
            else int(self.max_tool_executions)
        )
        if max_tool_executions is not None and max_tool_executions < 1:
            raise ValueError("max_tool_executions must be positive when configured")
        run_timeout_seconds = int(self.run_timeout_seconds)
        if run_timeout_seconds < 1:
            raise ValueError("run_timeout_seconds must be positive")
        subagent_max_workers = int(self.subagent_max_workers)
        if not 1 <= subagent_max_workers <= 3:
            raise ValueError("subagent_max_workers must be between 1 and 3")
        provider_context_limit_tokens = int(self.provider_context_limit_tokens)
        compaction_reserve_tokens = int(self.compaction_reserve_tokens)
        compaction_keep_recent_tokens = int(self.compaction_keep_recent_tokens)
        if provider_context_limit_tokens <= max_new_tokens:
            raise ValueError(
                "provider context limit must exceed max_new_tokens"
            )
        if compaction_reserve_tokens < max_new_tokens:
            raise ValueError(
                "compaction reserve must be at least max_new_tokens"
            )
        if compaction_reserve_tokens >= provider_context_limit_tokens:
            raise ValueError(
                "compaction reserve must be smaller than the provider context limit"
            )
        available_after_reserve = (
            provider_context_limit_tokens - compaction_reserve_tokens
        )
        if not 1 <= compaction_keep_recent_tokens <= available_after_reserve:
            raise ValueError(
                "compaction keep_recent must fit below the compaction threshold"
            )
        return replace(
            self,
            approval_policy=str(self.approval_policy),
            max_tool_executions=max_tool_executions,
            max_new_tokens=max_new_tokens,
            read_only=bool(self.read_only),
            shell_env_allowlist=tuple(self.shell_env_allowlist),
            secret_env_names=frozenset(
                str(name).upper() for name in (self.secret_env_names or ())
            ),
            allowed_tools=_allowed_tools(self.allowed_tools),
            run_timeout_seconds=run_timeout_seconds,
            provider_context_limit_tokens=provider_context_limit_tokens,
            compaction_reserve_tokens=compaction_reserve_tokens,
            compaction_keep_recent_tokens=compaction_keep_recent_tokens,
            sandbox_image=str(self.sandbox_image),
            verification_command=(
                None
                if self.verification_command is None
                else str(self.verification_command)
            ),
            allowed_write_paths=_allowed_write_paths(self.allowed_write_paths),
            subagent_max_workers=subagent_max_workers,
        )
