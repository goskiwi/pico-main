"""Normalized configuration for one Pico runtime."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from types import MappingProxyType
from typing import Any

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
DEFAULT_FEATURE_FLAGS = {
    "working_memory": True,
    "project_memory": True,
    "context_reduction": True,
    "prompt_cache": True,
}


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
    from .subagents.contracts import normalize_relative_file

    normalized = tuple(normalize_relative_file(path) for path in value)
    if len(set(normalized)) != len(normalized):
        raise ValueError("allowed_write_paths must be unique")
    return normalized


@dataclass(frozen=True, slots=True)
class PicoConfig:
    """User-controlled runtime policy, limits, and feature switches."""

    approval_policy: str = "ask"
    max_steps: int | None = None
    max_new_tokens: int = 1024
    read_only: bool = False
    shell_env_allowlist: tuple[str, ...] = DEFAULT_SHELL_ENV_ALLOWLIST
    secret_env_names: frozenset[str] = field(default_factory=frozenset)
    feature_flags: Mapping[str, bool] = field(
        default_factory=lambda: MappingProxyType(dict(DEFAULT_FEATURE_FLAGS))
    )
    allowed_tools: tuple[str, ...] | None = None
    run_timeout_seconds: int = 600
    provider_context_limit_tokens: int = 64000
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
        max_new_tokens = max(1, int(self.max_new_tokens))
        max_steps = None if self.max_steps is None else max(1, int(self.max_steps))
        requested_feature_flags = {
            str(key): bool(value) for key, value in self.feature_flags.items()
        }
        unknown_feature_flags = sorted(
            set(requested_feature_flags) - set(DEFAULT_FEATURE_FLAGS)
        )
        if unknown_feature_flags:
            raise ValueError(
                "unknown feature flags: " + ", ".join(unknown_feature_flags)
            )
        feature_flags = {**DEFAULT_FEATURE_FLAGS, **requested_feature_flags}
        provider_context_limit_tokens = max(
            max_new_tokens + 1,
            int(self.provider_context_limit_tokens),
        )
        compaction_reserve_tokens = min(
            max(max_new_tokens, int(self.compaction_reserve_tokens)),
            max(max_new_tokens, provider_context_limit_tokens // 4),
        )
        compaction_keep_recent_tokens = min(
            max(1, int(self.compaction_keep_recent_tokens)),
            provider_context_limit_tokens - compaction_reserve_tokens,
        )
        return replace(
            self,
            approval_policy=str(self.approval_policy),
            max_steps=max_steps,
            max_new_tokens=max_new_tokens,
            read_only=bool(self.read_only),
            shell_env_allowlist=tuple(self.shell_env_allowlist),
            secret_env_names=frozenset(
                str(name).upper() for name in (self.secret_env_names or ())
            ),
            feature_flags=MappingProxyType(feature_flags),
            allowed_tools=_allowed_tools(self.allowed_tools),
            run_timeout_seconds=max(1, int(self.run_timeout_seconds)),
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
            subagent_max_workers=max(1, int(self.subagent_max_workers)),
        )
