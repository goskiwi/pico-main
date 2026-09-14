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
    max_model_requests_per_attempt: int = 32
    attempt_timeout_seconds: int = 3600
    context_limit_tokens: int | None = None
    max_output_tokens: int = 32000
    recent_history_tokens: int = 20000

    def __post_init__(self):
        if self.mode not in {"ask", "code", "auto"}:
            raise ValueError("mode must be ask, code, or auto")
        limits = {
            "max_model_requests_per_attempt": int(
                self.max_model_requests_per_attempt
            ),
            "attempt_timeout_seconds": int(self.attempt_timeout_seconds),
            "max_output_tokens": int(self.max_output_tokens),
            "recent_history_tokens": int(self.recent_history_tokens),
        }
        if any(value < 1 for value in limits.values()):
            raise ValueError("runtime limits must be positive")
        context_limit = (
            None
            if self.context_limit_tokens is None
            else int(self.context_limit_tokens)
        )
        if context_limit is not None:
            if context_limit < 1:
                raise ValueError("runtime limits must be positive")
            if context_limit <= limits["max_output_tokens"]:
                raise ValueError("context limit must exceed max_output_tokens")
            available = context_limit - limits["max_output_tokens"]
            if limits["recent_history_tokens"] > available:
                raise ValueError(
                    "recent history must fit below the compaction threshold"
                )
        normalized = {
            "mode": str(self.mode),
            **limits,
            "context_limit_tokens": context_limit,
            "allowed_tools": _allowed_tools(self.allowed_tools),
            "allowed_write_paths": _allowed_write_paths(self.allowed_write_paths),
        }
        for name, value in normalized.items():
            object.__setattr__(self, name, value)
