"""多步 agent 运行时使用的轻量工作记忆。

session history 负责保存完整事件流；这个模块只保存更小的一层工作集：
当前任务摘要、最近接触的文件、文件短摘要，以及少量跨轮笔记。
这样下一轮 prompt 还能接上上一轮，但不会被整段历史塞满。
"""

import hashlib
from datetime import datetime, timezone
import re
from pathlib import Path

from .config import (
    WORKING_FILE_LIMIT,
    EPISODIC_NOTE_LIMIT,
    FILE_SUMMARY_LIMIT,
    MAX_MEMORY_INDEX_LINES,
    MAX_MEMORY_INDEX_BYTES,
    STALE_DURABLE_MEMORY_DAYS,
)
from .workspace import clip, now

DURABLE_MEMORY_TYPES = {
    "user": {
        "title": "User Memory",
        "summary": "Stable user profile facts.",
        "tags": ["user"],
    },
    "feedback": {
        "title": "Feedback Memory",
        "summary": "User feedback and behavior preferences.",
        "tags": ["feedback"],
    },
    "project": {
        "title": "Project Memory",
        "summary": "Project decisions, constraints, and dynamics.",
        "tags": ["project"],
    },
    "reference": {
        "title": "Reference Memory",
        "summary": "External pointers and stable lookup references.",
        "tags": ["reference"],
    },
}


def default_memory_state():
    # 用一个小而结构化的状态，而不是一大段自由文本摘要。
    return {
        "working": {
            "goal": "",
            "current_subtask": "",
            "next_action": "",
            "last_error": "",
            "recent_files": [],
        },
        "episodic_notes": [],
        "file_summaries": {},
        "next_note_index": 0,
        "durable_types": [],
    }


class DurableMemoryStore:
    def __init__(self, root):
        self.root = Path(root)
        self.index_path = self.root / "MEMORY.md"
        self.entries_dir = self.root / "entries"

    def type_slugs(self):
        return [entry["type"] for entry in self.load_index()]

    def _read_index_lines(self):
        if not self.index_path.exists():
            return [], False
        data = self.index_path.read_bytes()
        truncated = len(data) > MAX_MEMORY_INDEX_BYTES
        data = data[:MAX_MEMORY_INDEX_BYTES]
        text = data.decode("utf-8", errors="ignore")
        lines = text.splitlines()
        if len(lines) > MAX_MEMORY_INDEX_LINES:
            truncated = True
            lines = lines[:MAX_MEMORY_INDEX_LINES]
        return lines, truncated

    def load_index(self):
        lines, truncated = self._read_index_lines()
        entries = []
        current = None
        for raw in lines:
            line = raw.strip()
            match = re.match(r"- \[([^\]]+)\]\((entries/[^)]+)\):\s*(.+)", line)
            if match:
                current = {
                    "type": match.group(1).strip(),
                    "path": match.group(2).strip(),
                    "title": match.group(3).strip(),
                    "summary": "",
                    "tags": [],
                    "truncated": truncated,
                }
                if current["type"] in DURABLE_MEMORY_TYPES:
                    entries.append(current)
                continue
            if current is None:
                continue
            summary_match = re.match(r"- summary:\s*(.+)", line)
            if summary_match:
                current["summary"] = summary_match.group(1).strip()
                continue
            tags_match = re.match(r"- tags:\s*(.+)", line)
            if tags_match:
                current["tags"] = [tag.strip() for tag in tags_match.group(1).split(",") if tag.strip()]
        return entries

    def load_type_notes(self, memory_type):
        path = self.entries_dir / f"{memory_type}.md"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        notes = []
        capture = False
        meta = {}
        current = None
        for raw in lines:
            line = raw.strip()
            if capture and current is not None and line.startswith("- description:"):
                current["description"] = line.split(":", 1)[1].strip()
            elif capture and current is not None and line.startswith("- created_at:"):
                current["created_at"] = line.split(":", 1)[1].strip()
            elif capture and current is not None and line.startswith("- updated_at:"):
                current["updated_at"] = line.split(":", 1)[1].strip()
            elif capture and current is not None and line.startswith("- tags:"):
                current["tags"] = [tag.strip() for tag in line.split(":", 1)[1].split(",") if tag.strip()]
            elif capture and current is not None and line.startswith("- text:"):
                current["text"] = line.split(":", 1)[1].strip()
            elif not capture and line.startswith("- tags:"):
                meta["tags"] = [tag.strip() for tag in line.split(":", 1)[1].split(",") if tag.strip()]
            elif not capture and line.startswith("- updated_at:"):
                meta["updated_at"] = line.split(":", 1)[1].strip()
            elif line == "## Notes":
                capture = True
            elif capture and line.startswith("### "):
                if current:
                    notes.append(current)
                current = {
                    "name": line[4:].strip(),
                    "description": "",
                    "text": "",
                    "tags": list(meta.get("tags", [])),
                    "source": memory_type,
                    "created_at": meta.get("updated_at", "") or now(),
                    "updated_at": meta.get("updated_at", "") or now(),
                    "kind": "durable",
                    "type": memory_type,
                }
            elif capture and line.startswith("- "):
                text = line[2:].strip()
                notes.append(
                    {
                        "name": _slugify(text),
                        "description": text,
                        "text": text,
                        "tags": list(meta.get("tags", [])),
                        "source": memory_type,
                        "created_at": meta.get("updated_at", "") or now(),
                        "updated_at": meta.get("updated_at", "") or now(),
                        "kind": "durable",
                        "type": memory_type,
                    }
                )
        if current:
            notes.append(current)
        return notes

    @staticmethod
    def _subject_key(text):
        text = str(text).strip()
        patterns = (
            r"^(.+?)\s+is\s+.+$",
            r"^(.+?)\s+are\s+.+$",
            r"^(.+?)\s+uses?\s+.+$",
            r"^(.+?)\s+should\s+.+$",
            r"^(.+?)是.+$",
            r"^(.+?)使用.+$",
        )
        for pattern in patterns:
            match = re.match(pattern, text, re.I)
            if match:
                subject = " ".join(_tokenize(match.group(1)))
                return subject or None
        return None

    def retrieval_candidates(self, query, limit=3):
        query_tokens = _tokenize(query)
        ranked = []
        for entry in self.load_index():
            notes = self.load_type_notes(entry["type"])
            for note in notes:
                note_tags = {tag.lower() for tag in note.get("tags", [])}
                note_tokens = (
                    _tokenize(note.get("text", ""))
                    | _tokenize(note.get("description", ""))
                    | _tokenize(entry.get("title", ""))
                    | note_tags
                )
                exact_tag_match = int(bool(query_tokens & note_tags))
                keyword_overlap = len(query_tokens & note_tokens)
                if exact_tag_match == 0 and keyword_overlap == 0:
                    continue
                recency = _parse_timestamp(note.get("created_at"))
                ranked.append(((exact_tag_match, keyword_overlap, recency), note))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [note for _, note in ranked[:limit]]

    def _write_index(self, entries):
        self.root.mkdir(parents=True, exist_ok=True)
        self.entries_dir.mkdir(parents=True, exist_ok=True)
        lines = ["# Durable Memory Index", ""]
        for entry in entries:
            lines.append(f"- [{entry['type']}](entries/{entry['type']}.md): {entry['title']}")
            lines.append(f"  - summary: {entry['summary']}")
            lines.append(f"  - tags: {', '.join(entry['tags'])}")
        self.index_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def _write_type(self, memory_type, notes):
        self.entries_dir.mkdir(parents=True, exist_ok=True)
        meta = DURABLE_MEMORY_TYPES[memory_type]
        lines = [
            f"# {meta['title']}",
            "",
            f"- type: {memory_type}",
            f"- summary: {meta['summary']}",
            f"- tags: {', '.join(meta['tags'])}",
            f"- updated_at: {now()}",
            "",
            "## Notes",
        ]
        for note in notes:
            lines.extend(
                [
                    f"### {note['name']}",
                    f"- description: {note['description']}",
                    f"- created_at: {note['created_at']}",
                    f"- updated_at: {note['updated_at']}",
                    f"- tags: {', '.join(note['tags'])}",
                    f"- text: {note['text']}",
                    "",
                ]
            )
        (self.entries_dir / f"{memory_type}.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def promote(self, promotions):
        if not promotions:
            return [], []
        entries = {entry["type"]: entry for entry in self.load_index()}
        type_notes = {slug: self.load_type_notes(slug) for slug in entries}
        results = []
        superseded = []
        for memory_type, note_text in promotions:
            meta = DURABLE_MEMORY_TYPES[memory_type]
            entries.setdefault(
                memory_type,
                {
                    "type": memory_type,
                    "title": meta["title"],
                    "summary": meta["summary"],
                    "tags": list(meta["tags"]),
                },
            )
            existing = type_notes.setdefault(memory_type, [])
            if any(note["text"] == note_text for note in existing):
                continue
            new_subject = self._subject_key(note_text)
            timestamp = now()
            new_note = {
                "name": _slugify(note_text),
                "description": clip(note_text, 120),
                "text": note_text,
                "tags": list(meta["tags"]),
                "source": memory_type,
                "created_at": timestamp,
                "updated_at": timestamp,
                "kind": "durable",
                "type": memory_type,
            }
            replaced = False
            if new_subject:
                for index, old_text in enumerate(list(existing)):
                    if self._subject_key(old_text.get("text", "")) == new_subject:
                        superseded.append(f"{memory_type}: {old_text['text']} -> {note_text}")
                        new_note["created_at"] = old_text.get("created_at", timestamp)
                        existing[index] = new_note
                        replaced = True
                        break
            if not replaced:
                existing.append(new_note)
            results.append(f"{memory_type}: {note_text}")
        self._write_index([entries[slug] for slug in DURABLE_MEMORY_TYPES if slug in entries])
        for memory_type, notes in type_notes.items():
            self._write_type(memory_type, notes)
        return results, superseded


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
    if resolved is None:
        return Path(str(raw_path)).as_posix()
    if workspace_root is None:
        return Path(str(raw_path)).as_posix()
    root = Path(workspace_root).resolve()
    return resolved.relative_to(root).as_posix()


def file_freshness(raw_path, workspace_root=None):
    resolved = resolve_workspace_path(raw_path, workspace_root)
    if resolved is None or not resolved.exists() or not resolved.is_file():
        return None
    return hashlib.sha256(resolved.read_bytes()).hexdigest()


def _tokenize(text):
    text = str(text)
    tokens = {token.lower() for token in re.findall(r"[A-Za-z0-9_]+", text)}

    cjk_span = []

    def flush_cjk_span():
        if len(cjk_span) == 1:
            tokens.add(cjk_span[0])
        elif cjk_span:
            # Bigrams make unsegmented Chinese phrases comparable without making
            # every shared common character a lexical match.
            tokens.update("".join(cjk_span[index : index + 2]) for index in range(len(cjk_span) - 1))
        cjk_span.clear()

    for character in text:
        codepoint = ord(character)
        if (
            0x3400 <= codepoint <= 0x4DBF
            or 0x4E00 <= codepoint <= 0x9FFF
            or 0xF900 <= codepoint <= 0xFAFF
            or 0x20000 <= codepoint <= 0x2FA1F
            or 0x30000 <= codepoint <= 0x323AF
        ):
            cjk_span.append(character)
        else:
            flush_cjk_span()
    flush_cjk_span()
    return tokens


def _parse_timestamp(value):
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except Exception:
        return 0.0


def _age_days(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - parsed).days)


def _slugify(text, limit=48):
    tokens = re.findall(r"[A-Za-z0-9]+", str(text).lower())
    if tokens:
        slug = "-".join(tokens)[:limit].strip("-")
        if slug:
            return slug
    digest = hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:12]
    return f"memory-{digest}"


def render_relevant_memory_note(note):
    text = str(note.get("text", "")).strip()
    if not text:
        return ""
    if str(note.get("kind", "")).strip() != "durable":
        return text
    age = _age_days(note.get("updated_at") or note.get("created_at"))
    if age is None or age < STALE_DURABLE_MEMORY_DAYS:
        return text
    return f"[saved {age} days ago; verify before acting] {text}"


def _normalize_note(note, index):
    if isinstance(note, str):
        text = clip(note.strip(), 500)
        return {
            "text": text,
            "tags": [],
            "source": "",
            "created_at": now(),
            "note_index": index,
            "kind": "episodic",
        }

    if not isinstance(note, dict):
        text = clip(str(note).strip(), 500)
        return {
            "text": text,
            "tags": [],
            "source": "",
            "created_at": now(),
            "note_index": index,
            "kind": "episodic",
        }

    text = clip(str(note.get("text", "")).strip(), 500)
    tags = [str(tag).strip() for tag in _ensure_list(note.get("tags", [])) if str(tag).strip()]
    source = str(note.get("source", "")).strip()
    created_at = str(note.get("created_at", "")).strip() or now()
    note_index = int(note.get("note_index", index))
    kind = str(note.get("kind", "episodic")).strip() or "episodic"
    return {
        "text": text,
        "tags": _dedupe_preserve_order(tags),
        "source": source,
        "created_at": created_at,
        "note_index": note_index,
        "kind": kind,
    }


def normalize_memory_state(state, workspace_root=None):
    if state is None:
        state = default_memory_state()
    elif not isinstance(state, dict):
        raise TypeError("memory state must be a mapping")

    # 规范化层只接受当前版本的记忆结构；旧 flat 字段不再回填。
    working = state.get("working")
    if not isinstance(working, dict):
        working = {}
    for field, limit in (
        ("goal", 300),
        ("current_subtask", 240),
        ("next_action", 240),
        ("last_error", 240),
    ):
        working[field] = clip(str(working.get(field, "")).strip(), limit)
    working["recent_files"] = _dedupe_preserve_order(
        [
            canonicalize_path(path, workspace_root)
            for path in _ensure_list(working.get("recent_files", []))
            if str(path).strip()
        ]
    )[-WORKING_FILE_LIMIT:]
    state["working"] = working

    episodic_notes = state.get("episodic_notes")
    if not isinstance(episodic_notes, list):
        episodic_notes = []

    normalized_notes = []
    for index, note in enumerate(episodic_notes):
        if isinstance(note, str) and not str(note).strip():
            continue
        normalized_notes.append(_normalize_note(note, index))
    episodic_notes = normalized_notes
    episodic_notes = episodic_notes[-EPISODIC_NOTE_LIMIT:]
    state["episodic_notes"] = episodic_notes

    file_summaries = state.get("file_summaries")
    if not isinstance(file_summaries, dict):
        file_summaries = {}
    normalized_file_summaries = {}
    for path, summary in file_summaries.items():
        path = canonicalize_path(path, workspace_root)
        if isinstance(summary, dict):
            text = clip(str(summary.get("summary", "")).strip(), 500)
            created_at = str(summary.get("created_at", "")).strip() or now()
            freshness = summary.get("freshness")
            freshness = None if freshness in (None, "") else str(freshness).strip() or None
        else:
            text = clip(str(summary).strip(), 500)
            created_at = now()
            freshness = None
        if not path or not text:
            continue
        normalized_file_summaries[path] = {
            "summary": text,
            "created_at": created_at,
            "freshness": freshness,
        }
    state["file_summaries"] = normalized_file_summaries

    next_note_index = state.get("next_note_index")
    if not isinstance(next_note_index, int) or next_note_index < 0:
        next_note_index = 0
    max_index = max([note["note_index"] for note in episodic_notes], default=-1)
    state["next_note_index"] = max(next_note_index, max_index + 1)

    durable_root = Path(workspace_root) / ".pico" / "memory" if workspace_root is not None else None
    durable_store = DurableMemoryStore(durable_root) if durable_root is not None else None
    state["durable_types"] = durable_store.type_slugs() if durable_store is not None else []
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


def append_note(state, text, tags=(), source="", created_at=None, workspace_root=None, kind="episodic"):
    state = normalize_memory_state(state, workspace_root)
    text = clip(str(text).strip(), 500)
    if not text:
        return state

    normalized_tags = _dedupe_preserve_order(
        [str(tag).strip() for tag in _ensure_list(tags) if str(tag).strip()]
    )
    note = {
        "text": text,
        "tags": normalized_tags,
        "source": str(source).strip(),
        "created_at": str(created_at).strip() if created_at else now(),
        "note_index": int(state.get("next_note_index", 0)),
        "kind": str(kind).strip() or "episodic",
    }
    state["next_note_index"] = note["note_index"] + 1

    notes = [item for item in state["episodic_notes"] if item["text"] != note["text"]]
    notes.append(note)
    state["episodic_notes"] = notes[-EPISODIC_NOTE_LIMIT:]
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
    if not path:
        return state
    state["file_summaries"].pop(path, None)
    return state


def invalidate_stale_file_summaries(state, workspace_root=None):
    state = normalize_memory_state(state, workspace_root)
    invalidated = []
    for path, summary in list(state["file_summaries"].items()):
        current_freshness = file_freshness(path, workspace_root)
        if summary.get("freshness") == current_freshness:
            continue
        invalidated.append(path)
        state["file_summaries"].pop(path, None)
    return state, invalidated


def summarize_read_result(result, limit=180):
    # 我们不会把完整文件内容塞进记忆层，
    # 这里只保留足够提醒下一轮“刚刚读到了什么”的短摘要。
    lines = [line.strip() for line in str(result).splitlines() if line.strip()]
    if not lines:
        return "(empty)"
    if lines[0].startswith("# "):
        lines = lines[1:]
    if not lines:
        return "(empty)"
    summary = " | ".join(lines[:3])
    return clip(summary, limit)


def retrieval_candidates(state, query, limit=3, workspace_root=None):
    state = normalize_memory_state(state, workspace_root)
    query_tokens = _tokenize(query)
    ranked = []
    for note in state["episodic_notes"]:
        # 召回逻辑故意保持简单透明：先看 tag 精确命中，
        # 再看关键词重叠，最后看新旧程度。这里不引入 embedding。
        note_tags = {tag.lower() for tag in note.get("tags", [])}
        note_tokens = _tokenize(note.get("text", "")) | _tokenize(note.get("source", "")) | note_tags
        exact_tag_match = int(bool(query_tokens & note_tags))
        keyword_overlap = len(query_tokens & note_tokens)
        if exact_tag_match == 0 and keyword_overlap == 0:
            continue
        recency = _parse_timestamp(note.get("created_at"))
        note_index = int(note.get("note_index", 0))
        ranked.append(((exact_tag_match, keyword_overlap, recency, note_index), note))

    if workspace_root is not None:
        durable_store = DurableMemoryStore(Path(workspace_root) / ".pico" / "memory")
        for note in durable_store.retrieval_candidates(query, limit=limit):
            note_tags = {tag.lower() for tag in note.get("tags", [])}
            note_tokens = _tokenize(note.get("text", "")) | _tokenize(note.get("source", "")) | note_tags
            exact_tag_match = int(bool(query_tokens & note_tags))
            keyword_overlap = len(query_tokens & note_tokens)
            recency = _parse_timestamp(note.get("created_at"))
            ranked.append(((exact_tag_match, keyword_overlap, recency, -1), note))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return [note for _, note in ranked[:limit]]


def retrieval_view(state, query, limit=3, workspace_root=None):
    candidates = retrieval_candidates(state, query, limit=limit, workspace_root=workspace_root)
    lines = ["Relevant memory:"]
    if not candidates:
        lines.append("- none")
        return "\n".join(lines)
    for note in candidates:
        rendered_note = render_relevant_memory_note(note)
        if rendered_note:
            lines.append(f"- {rendered_note}")
    return "\n".join(lines)


def render_memory_text(state, workspace_root=None):
    state = normalize_memory_state(state, workspace_root)
    working = state["working"]
    file_summary_lines = []
    for path in working["recent_files"][:FILE_SUMMARY_LIMIT]:
        summary = state["file_summaries"].get(path, {})
        current_freshness = file_freshness(path, workspace_root)
        if summary.get("summary", "") and summary.get("freshness") == current_freshness:
            file_summary_lines.append(f"- {path}: {summary['summary']}")

    note_lines = []
    for note in state["episodic_notes"][-EPISODIC_NOTE_LIMIT:]:
        rendered = render_relevant_memory_note(note)
        if rendered:
            note_lines.append(f"- {rendered}")

    recent_file_lines = [f"- {path}" for path in working["recent_files"]] or ["- none"]
    durable_types = state.get("durable_types", [])
    sections = [
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
        "Episodic Notes",
        *(note_lines or ["- none"]),
        "",
        "Durable Types",
        f"- {', '.join(durable_types) or '-'}",
    ]
    return "\n".join(sections).strip()


class LayeredMemory:
    def __init__(self, state=None, workspace_root=None):
        self.workspace_root = workspace_root
        self.state = normalize_memory_state(state, workspace_root)
        self.durable_store = DurableMemoryStore(Path(workspace_root) / ".pico" / "memory") if workspace_root is not None else None

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

    def append_note(self, text, tags=(), source="", created_at=None, kind="episodic"):
        self.state = append_note(
            self.state,
            text,
            tags=tags,
            source=source,
            created_at=created_at,
            workspace_root=self.workspace_root,
            kind=kind,
        )
        return self

    def set_file_summary(self, path, summary):
        self.state = set_file_summary(self.state, path, summary, self.workspace_root)
        return self

    def invalidate_file_summary(self, path):
        self.state = invalidate_file_summary(self.state, path, self.workspace_root)
        return self

    def invalidate_stale_file_summaries(self):
        self.state, invalidated = invalidate_stale_file_summaries(self.state, self.workspace_root)
        return invalidated

    def retrieval_candidates(self, query, limit=3):
        return retrieval_candidates(self.state, query, limit=limit, workspace_root=self.workspace_root)

    def retrieval_view(self, query, limit=3):
        return retrieval_view(self.state, query, limit=limit, workspace_root=self.workspace_root)

    def render_memory_text(self):
        return render_memory_text(self.state, self.workspace_root)

    def promote_durable(self, promotions):
        if self.durable_store is None:
            return [], []
        self.state = normalize_memory_state(self.state, self.workspace_root)
        promoted, superseded = self.durable_store.promote(promotions)
        self.state = normalize_memory_state(self.state, self.workspace_root)
        return promoted, superseded
