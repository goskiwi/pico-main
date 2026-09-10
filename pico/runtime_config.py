"""Normalized configuration for one Pico runtime."""

from __future__ import annotations

from dataclasses import dataclass, field

from .workspace import normalize_relative_file


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

    mode: str = "code"
    max_agent_turns: int = 32
    max_parallel_tools: int = 4
    max_new_tokens: int = 32000
    secret_env_names: frozenset[str] = field(default_factory=frozenset)
    allowed_tools: tuple[str, ...] | None = None
    turn_timeout_seconds: int = 600
    provider_context_limit_tokens: int = 272000
    compaction_reserve_tokens: int = 32000
    compaction_keep_recent_tokens: int = 20000
    summary_max_output_tokens: int = 16000
    verification_command: str = ""
    allowed_write_paths: tuple[str, ...] | None = None
    memory_enabled: bool = True

    def __post_init__(self):
        if not isinstance(self.memory_enabled, bool):
            raise TypeError("memory_enabled must be a boolean")
        if self.mode not in {"ask", "code", "auto"}:
            raise ValueError("mode must be ask, code, or auto")
        if not isinstance(self.verification_command, str):
            raise TypeError("verification_command must be a string")
        max_new_tokens = int(self.max_new_tokens)
        if max_new_tokens < 1:
            raise ValueError("max_new_tokens must be positive")
        max_agent_turns = int(self.max_agent_turns)
        if max_agent_turns < 1:
            raise ValueError("max_agent_turns must be positive")
        if type(self.max_parallel_tools) is not int or not 1 <= self.max_parallel_tools <= 4:
            raise ValueError("max_parallel_tools must be between 1 and 4")
        turn_timeout_seconds = int(self.turn_timeout_seconds)
        if turn_timeout_seconds < 1:
            raise ValueError("turn_timeout_seconds must be positive")
        provider_context_limit_tokens = int(self.provider_context_limit_tokens)
        compaction_reserve_tokens = int(self.compaction_reserve_tokens)
        compaction_keep_recent_tokens = int(self.compaction_keep_recent_tokens)
        summary_max_output_tokens = int(self.summary_max_output_tokens)
        if summary_max_output_tokens < 1:
            raise ValueError("summary_max_output_tokens must be positive")
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
        normalized = {
            "mode": str(self.mode),
            "max_agent_turns": max_agent_turns,
            "max_new_tokens": max_new_tokens,
            "secret_env_names": frozenset(
                str(name).upper() for name in (self.secret_env_names or ())
            ),
            "allowed_tools": _allowed_tools(self.allowed_tools),
            "turn_timeout_seconds": turn_timeout_seconds,
            "provider_context_limit_tokens": provider_context_limit_tokens,
            "compaction_reserve_tokens": compaction_reserve_tokens,
            "compaction_keep_recent_tokens": compaction_keep_recent_tokens,
            "summary_max_output_tokens": summary_max_output_tokens,
            "verification_command": self.verification_command,
            "allowed_write_paths": _allowed_write_paths(self.allowed_write_paths),
        }

        for name, value in normalized.items():
            object.__setattr__(self, name, value)
