"""Small, session-scoped working memory for the agent runtime.

Run artifacts preserve the complete, auditable event stream.  This module keeps
only the current task state, recently touched files, short file summaries, and
small process notes needed while a session is active.  It deliberately does not
perform cross-session retrieval or background memory extraction.
"""

import hashlib
from pathlib import Path

from .config import EPISODIC_NOTE_LIMIT, FILE_SUMMARY_LIMIT, WORKING_FILE_LIMIT
from .workspace import clip, now


def default_memory_state():
    return {
        "working": {
            "goal": "",
            "current_subtask": "",
            "next_action": "",
            "last_error": "",
            "recent_files": [],
        },
        "process_notes": [],
        "file_summaries": {},
        "next_note_index": 0,
    }


def _ensure_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    if value in (None, ""):
        return []
    return [value]


def _dedupe_preserve_order(items):
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def resolve_workspace_path(raw_path, workspace_root=None):
    path = Path(str(raw_path))
    if workspace_root is None:
        return path

    root = Path(workspace_root).resolve()
    candidate = path if path.is_absolute() else root / path
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def canonicalize_path(raw_path, workspace_root=None):
    resolved = resolve_workspace_path(raw_path, workspace_root)
    if resolved is None or workspace_root is None:
        return Path(str(raw_path)).as_posix()
    return resolved.relative_to(Path(workspace_root).resolve()).as_posix()


def file_freshness(raw_path, workspace_root=None):
    resolved = resolve_workspace_path(raw_path, workspace_root)
    if resolved is None or not resolved.exists() or not resolved.is_file():
        return None
    return hashlib.sha256(resolved.read_bytes()).hexdigest()


def _normalize_note(note, index):
    if not isinstance(note, dict):
        raise ValueError(f"memory process_notes[{index}] must be an object")
    required_fields = ("text", "tags", "source", "created_at", "note_index")
    missing = [field for field in required_fields if field not in note]
    if missing:
        raise ValueError(
            f"memory process_notes[{index}] missing required fields: {', '.join(missing)}"
        )
    if not isinstance(note["text"], str):
        raise ValueError(f"memory process_notes[{index}].text must be a string")
    if not isinstance(note["tags"], list) or not all(
        isinstance(tag, str) for tag in note["tags"]
    ):
        raise ValueError(f"memory process_notes[{index}].tags must be a list of strings")
    if not isinstance(note["source"], str):
        raise ValueError(f"memory process_notes[{index}].source must be a string")
    if not isinstance(note["created_at"], str) or not note["created_at"].strip():
        raise ValueError(f"memory process_notes[{index}].created_at must be a non-empty string")
    if not isinstance(note["note_index"], int) or isinstance(note["note_index"], bool):
        raise ValueError(f"memory process_notes[{index}].note_index must be an integer")

    return {
        "text": clip(note["text"].strip(), 500),
        "tags": _dedupe_preserve_order(
            [tag.strip() for tag in note["tags"] if tag.strip()]
        ),
        "source": note["source"].strip(),
        "created_at": note["created_at"].strip(),
        "note_index": note["note_index"],
    }


def normalize_memory_state(state, workspace_root=None):
    if state is None:
        state = default_memory_state()
    elif not isinstance(state, dict):
        raise TypeError("memory state must be a mapping")

    required_fields = ("working", "process_notes", "file_summaries", "next_note_index")
    missing = [field for field in required_fields if field not in state]
    if missing:
        raise ValueError(f"memory state missing required fields: {', '.join(missing)}")

    working = state["working"]
    if not isinstance(working, dict):
        raise ValueError("memory working must be an object")
    for field, limit in (
        ("goal", 300),
        ("current_subtask", 240),
        ("next_action", 240),
        ("last_error", 240),
    ):
        if not isinstance(working.get(field), str):
            raise ValueError(f"memory working.{field} must be a string")
        working[field] = clip(working[field].strip(), limit)
    if not isinstance(working.get("recent_files"), list) or not all(
        isinstance(path, str) for path in working["recent_files"]
    ):
        raise ValueError("memory working.recent_files must be a list of strings")
    working["recent_files"] = _dedupe_preserve_order(
        [
            canonicalize_path(path, workspace_root)
            for path in working["recent_files"]
            if path.strip()
        ]
    )[-WORKING_FILE_LIMIT:]
    state["working"] = working

    process_notes = state["process_notes"]
    if not isinstance(process_notes, list):
        raise ValueError("memory process_notes must be a list")
    state["process_notes"] = [
        _normalize_note(note, index)
        for index, note in enumerate(process_notes)
    ][-EPISODIC_NOTE_LIMIT:]

    file_summaries = state["file_summaries"]
    if not isinstance(file_summaries, dict):
        raise ValueError("memory file_summaries must be an object")
    normalized_file_summaries = {}
    for path, summary in file_summaries.items():
        if not isinstance(path, str) or not path.strip():
            raise ValueError("memory file_summaries keys must be non-empty strings")
        if not isinstance(summary, dict):
            raise ValueError(f"memory file_summaries[{path!r}] must be an object")
        required_summary_fields = ("summary", "created_at", "freshness")
        missing_summary_fields = [
            field for field in required_summary_fields if field not in summary
        ]
        if missing_summary_fields:
            raise ValueError(
                f"memory file_summaries[{path!r}] missing required fields: "
                f"{', '.join(missing_summary_fields)}"
            )
        if not isinstance(summary["summary"], str):
            raise ValueError(f"memory file_summaries[{path!r}].summary must be a string")
        if not isinstance(summary["created_at"], str) or not summary["created_at"].strip():
            raise ValueError(
                f"memory file_summaries[{path!r}].created_at must be a non-empty string"
            )
        if summary["freshness"] is not None and not isinstance(summary["freshness"], str):
            raise ValueError(
                f"memory file_summaries[{path!r}].freshness must be a string or null"
            )
        canonical_path = canonicalize_path(path, workspace_root)
        text = clip(summary["summary"].strip(), 500)
        freshness = summary["freshness"]
        freshness = None if freshness in (None, "") else freshness.strip() or None
        if not canonical_path or not text:
            raise ValueError(
                "memory file_summaries entries require a canonical path and summary"
            )
        normalized_file_summaries[canonical_path] = {
            "summary": text,
            "created_at": summary["created_at"].strip(),
            "freshness": freshness,
        }
    state["file_summaries"] = normalized_file_summaries

    next_note_index = state["next_note_index"]
    if (
        not isinstance(next_note_index, int)
        or isinstance(next_note_index, bool)
        or next_note_index < 0
    ):
        raise ValueError("memory next_note_index must be a non-negative integer")
    max_index = max([note["note_index"] for note in state["process_notes"]], default=-1)
    state["next_note_index"] = max(next_note_index, max_index + 1)
    return state


def set_goal(state, goal, workspace_root=None):
    state = normalize_memory_state(state, workspace_root)
    state["working"]["goal"] = clip(str(goal).strip(), 300)
    return state


def update_working_state(state, workspace_root=None, **updates):
    state = normalize_memory_state(state, workspace_root)
    limits = {
        "goal": 300,
        "current_subtask": 240,
        "next_action": 240,
        "last_error": 240,
    }
    for key, limit in limits.items():
        if key in updates:
            state["working"][key] = clip(str(updates[key]).strip(), limit)
    return state


def remember_file(state, path, workspace_root=None):
    state = normalize_memory_state(state, workspace_root)
    path = canonicalize_path(path, workspace_root).strip()
    if not path:
        return state
    files = [item for item in state["working"]["recent_files"] if item != path]
    files.append(path)
    state["working"]["recent_files"] = files[-WORKING_FILE_LIMIT:]
    return state


def append_note(state, text, tags=(), source="", created_at=None, workspace_root=None):
    state = normalize_memory_state(state, workspace_root)
    text = clip(str(text).strip(), 500)
    if not text:
        return state

    note = {
        "text": text,
        "tags": _dedupe_preserve_order(
            [str(tag).strip() for tag in _ensure_list(tags) if str(tag).strip()]
        ),
        "source": str(source).strip(),
        "created_at": str(created_at).strip() if created_at else now(),
        "note_index": int(state["next_note_index"]),
    }
    state["next_note_index"] = note["note_index"] + 1
    notes = [item for item in state["process_notes"] if item["text"] != note["text"]]
    notes.append(note)
    state["process_notes"] = notes[-EPISODIC_NOTE_LIMIT:]
    return state


def set_file_summary(state, path, summary, workspace_root=None):
    state = normalize_memory_state(state, workspace_root)
    path = canonicalize_path(path, workspace_root).strip()
    summary = clip(str(summary).strip(), 500)
    if not path or not summary:
        return state
    state["file_summaries"][path] = {
        "summary": summary,
        "created_at": now(),
        "freshness": file_freshness(path, workspace_root),
    }
    return state


def invalidate_file_summary(state, path, workspace_root=None):
    state = normalize_memory_state(state, workspace_root)
    path = canonicalize_path(path, workspace_root).strip()
    if path:
        state["file_summaries"].pop(path, None)
    return state


def invalidate_stale_file_summaries(state, workspace_root=None):
    state = normalize_memory_state(state, workspace_root)
    invalidated = []
    for path, summary in list(state["file_summaries"].items()):
        if summary.get("freshness") == file_freshness(path, workspace_root):
            continue
        invalidated.append(path)
        state["file_summaries"].pop(path, None)
    return state, invalidated


def summarize_read_result(result, limit=180):
    lines = [line.strip() for line in str(result).splitlines() if line.strip()]
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    if not lines:
        return "(empty)"
    return clip(" | ".join(lines[:3]), limit)


def render_memory_text(state, workspace_root=None):
    state = normalize_memory_state(state, workspace_root)
    working = state["working"]
    file_summary_lines = []
    for path in working["recent_files"][:FILE_SUMMARY_LIMIT]:
        summary = state["file_summaries"].get(path, {})
        if summary.get("summary") and summary.get("freshness") == file_freshness(
            path, workspace_root
        ):
            file_summary_lines.append(f"- {path}: {summary['summary']}")

    note_lines = [
        f"- {note['text']}"
        for note in state["process_notes"][-EPISODIC_NOTE_LIMIT:]
        if note.get("text")
    ]
    recent_file_lines = [f"- {path}" for path in working["recent_files"]] or ["- none"]
    return "\n".join(
        [
            "Working",
            f"- goal: {working['goal'] or '-'}",
            f"- current_subtask: {working['current_subtask'] or '-'}",
            f"- next_action: {working['next_action'] or '-'}",
            f"- last_error: {working['last_error'] or '-'}",
            "",
            "Recent Files",
            *recent_file_lines,
            "",
            "File Summaries",
            *(file_summary_lines or ["- none"]),
            "",
            "Process Notes",
            *(note_lines or ["- none"]),
        ]
    ).strip()


class LayeredMemory:
    def __init__(self, state=None, workspace_root=None):
        self.workspace_root = workspace_root
        self.state = normalize_memory_state(state, workspace_root)

    def to_dict(self):
        self.state = normalize_memory_state(self.state, self.workspace_root)
        return self.state

    def canonical_path(self, path):
        return canonicalize_path(path, self.workspace_root)

    def set_goal(self, goal):
        self.state = set_goal(self.state, goal, self.workspace_root)
        return self

    def update_working_state(self, **updates):
        self.state = update_working_state(self.state, self.workspace_root, **updates)
        return self

    def remember_file(self, path):
        self.state = remember_file(self.state, path, self.workspace_root)
        return self

    def append_note(self, text, tags=(), source="", created_at=None):
        self.state = append_note(
            self.state,
            text,
            tags=tags,
            source=source,
            created_at=created_at,
            workspace_root=self.workspace_root,
        )
        return self

    def set_file_summary(self, path, summary):
        self.state = set_file_summary(self.state, path, summary, self.workspace_root)
        return self

    def invalidate_file_summary(self, path):
        self.state = invalidate_file_summary(self.state, path, self.workspace_root)
        return self

    def invalidate_stale_file_summaries(self):
        self.state, invalidated = invalidate_stale_file_summaries(
            self.state, self.workspace_root
        )
        return invalidated

    def render_memory_text(self):
        return render_memory_text(self.state, self.workspace_root)
