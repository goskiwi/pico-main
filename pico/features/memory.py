"""Small session-scoped working state.

Repository facts, file recency, and recovery evidence belong to the workspace
and Run Journal. This object only keeps the current task goal visible while a
Session is active.
"""

from __future__ import annotations

from ..workspace import clip

SESSION_MEMORY_SCHEMA_VERSION = "session-working-memory-v2"


def default_memory_state():
    return {
        "schema_version": SESSION_MEMORY_SCHEMA_VERSION,
        "goal": "",
    }


def normalize_memory_state(state, workspace_root=None):
    del workspace_root
    if not isinstance(state, dict):
        raise TypeError("session working memory must be an object")
    if set(state) != {"schema_version", "goal"}:
        raise ValueError("invalid session working memory schema")
    if state.get("schema_version") != SESSION_MEMORY_SCHEMA_VERSION:
        raise ValueError("unsupported session working memory schema_version")
    if not isinstance(state["goal"], str):
        raise TypeError("session working memory goal must be a string")
    state["goal"] = clip(state["goal"].strip(), 1000)
    return state


class SessionWorkingMemory:
    def __init__(self, state=None, workspace_root=None):
        self.state = normalize_memory_state(
            default_memory_state() if state is None else state,
            workspace_root,
        )

    def to_dict(self):
        return normalize_memory_state(self.state)

    def set_goal(self, goal):
        self.state["goal"] = clip(str(goal).strip(), 1000)
        return self

    def render_panel(self):
        return "\n".join(
            [
                "Session working state:",
                f"- current task goal: {self.state['goal'] or '-'}",
            ]
        )
