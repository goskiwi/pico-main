"""Live JSONL trace events and terminal rendering for one Pico run."""

from __future__ import annotations

import json
import threading


TRACE_EVENT_NAMES = frozenset(
    {
        "model_start",
        "tool_start",
        "tool_end",
        "verifier_end",
        "run_end",
    }
)


def _elapsed_label(value):
    try:
        elapsed_ms = max(0, int(value))
    except (TypeError, ValueError):
        elapsed_ms = 0
    minutes, remainder = divmod(elapsed_ms, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{minutes:02d}:{seconds:02d}.{milliseconds // 10:02d}"


def _terminal_detail(event):
    event_name = event.get("event")
    if event_name == "model_start":
        return (
            f"attempt={event.get('attempt', '?')} "
            f"tool_steps={event.get('tool_steps', '?')}"
        )
    if event_name == "tool_start":
        return " ".join(
            part
            for part in (
                f"tool={event.get('tool', '?')}",
                str(event.get("target", "")).strip(),
            )
            if part
        )
    if event_name == "tool_end":
        return (
            f"tool={event.get('tool', '?')} "
            f"status={event.get('status', '?')} "
            f"duration={event.get('duration_ms', 0)}ms"
        )
    if event_name == "verifier_end":
        return (
            f"status={event.get('status', '?')} "
            f"duration={event.get('duration_ms', 0)}ms"
        )
    if event_name == "run_end":
        return (
            f"status={event.get('status', '?')} "
            f"steps={event.get('tool_steps', 0)} "
            f"duration={event.get('run_duration_ms', 0)}ms"
        )
    return ""


def format_terminal_event(event):
    """Return one concise, human-readable live trace line."""
    detail = _terminal_detail(event)
    suffix = f" {detail}" if detail else ""
    return f"[{_elapsed_label(event.get('elapsed_ms'))}] {event.get('event', '?')}{suffix}"


class TraceSink:
    """Mirror normalized run events to a terminal stream or JSONL target."""

    def __init__(self, mode, stream, *, close_stream=False):
        if mode not in {"terminal", "jsonl"}:
            raise ValueError("trace sink mode must be 'terminal' or 'jsonl'")
        self.mode = mode
        self.stream = stream
        self.close_stream = bool(close_stream)
        self._lock = threading.Lock()

    def emit(self, event):
        if self.mode == "jsonl":
            line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        else:
            line = format_terminal_event(event)
        with self._lock:
            self.stream.write(line + "\n")
            self.stream.flush()

    def close(self):
        if self.close_stream:
            self.stream.close()
