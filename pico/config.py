"""Normalized configuration for one Pico runtime."""

from __future__ import annotations

from dataclasses import dataclass

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
    mode: str = "code"
    allowed_tools: tuple[str, ...] | None = None
    allowed_write_paths: tuple[str, ...] | None = None
    max_agent_turns: int = 32
    run_timeout_seconds: int = 600
    context_limit_tokens: int = 272000
    max_output_tokens: int = 32000
    recent_history_tokens: int = 20000

    def __post_init__(self):
        if self.mode not in {"ask", "code", "auto"}:
            raise ValueError("mode must be ask, code, or auto")
        values = {
            "max_agent_turns": int(self.max_agent_turns),
            "run_timeout_seconds": int(self.run_timeout_seconds),
            "context_limit_tokens": int(self.context_limit_tokens),
            "max_output_tokens": int(self.max_output_tokens),
            "recent_history_tokens": int(self.recent_history_tokens),
        }
        if any(value < 1 for value in values.values()):
            raise ValueError("runtime limits must be positive")
        if values["context_limit_tokens"] <= values["max_output_tokens"]:
            raise ValueError("context limit must exceed max_output_tokens")
        available = values["context_limit_tokens"] - values["max_output_tokens"]
        if values["recent_history_tokens"] > available:
            raise ValueError("recent history must fit below the compaction threshold")
        normalized = {
            "mode": str(self.mode),
            **values,
            "allowed_tools": _allowed_tools(self.allowed_tools),
            "allowed_write_paths": _allowed_write_paths(self.allowed_write_paths),
        }
        for name, value in normalized.items():
            object.__setattr__(self, name, value)
