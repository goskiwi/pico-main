"""Project-scoped Markdown memories and a single-writer recovery journal."""

import fcntl
import json
import math
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .persistence import atomic_replace_bytes, atomic_write_json

MEMORY_FILENAME = r"^[a-z][a-z0-9_-]{0,79}\.md$"
MEMORY_MAX_BYTES = 64 * 1024


class MemoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(pattern=MEMORY_FILENAME)
    type: Literal["user", "feedback", "project", "reference"]
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=240)
    content: str = Field(min_length=1, max_length=10000)
    source_event_ids: list[str] = Field(min_length=1, max_length=10)

    @field_validator("name", "description", "content")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("memory text must not be blank")
        return value.strip()


class MemoryChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")
    updates: list[MemoryUpdate] = Field(max_length=10)
    deletes: list[str] = Field(max_length=10)


class MemoryStore:
    def __init__(self, workspace_root, redactor):
        self.root = Path(workspace_root) / ".pico" / "memory"
        self.redactor = redactor
        for path in (self.root.parent, self.root, self.root / "topics"):
            if path.is_symlink():
                raise ValueError("memory directory must not be a symlink")
            path.mkdir(parents=True, exist_ok=True)

    def _path(self, name, *, topic=False):
        parent = self.root / "topics" if topic else self.root
        if self.root.parent.is_symlink() or self.root.is_symlink() or parent.is_symlink():
            raise ValueError("memory directory must not be a symlink")
        if topic and not re.fullmatch(MEMORY_FILENAME, name):
            raise ValueError("invalid memory filename")
        path = parent / name
        if path.is_symlink():
            raise ValueError("memory file must not be a symlink")
        return path

    @contextmanager
    def writer(self, *, blocking=True):
        with self._path("worker.lock").open("a+b") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def state(self):
        path = self._path("processing.json")
        if not path.exists():
            return {"processed_runs": [], "requests": [], "pending": None, "failures": {}, "forgotten": {}}
        with path.open("rb") as source:
            raw = source.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("memory processing record exceeds size limit")
        state = json.loads(raw)
        if (not isinstance(state, dict) or set(state) != {"processed_runs", "requests", "pending", "failures", "forgotten"}
                or not isinstance(state["processed_runs"], list) or not isinstance(state["requests"], list)
                or not isinstance(state["failures"], dict) or not isinstance(state["forgotten"], dict)):
            raise ValueError("invalid memory processing record")
        return state

    def save_state(self, state):
        atomic_write_json(self._path("processing.json"), state)

    def read(self, filename):
        with self._path(filename, topic=True).open("rb") as source:
            raw = source.read(MEMORY_MAX_BYTES + 1)
        if len(raw) > MEMORY_MAX_BYTES:
            raise ValueError("memory topic exceeds size limit")
        text = raw.decode("utf-8")
        header, separator, content = text.removeprefix("---\n").partition("\n---\n")
        if not text.startswith("---\n") or not separator:
            raise ValueError("memory topic requires JSON frontmatter")
        metadata = json.loads(header)
        if not isinstance(metadata, dict) or set(metadata) != {"type", "name", "description", "source", "source_event_ids", "source_timestamp"}:
            raise ValueError("invalid memory topic metadata")
        if (not isinstance(metadata["source"], str) or type(metadata["source_timestamp"]) not in {int, float}
                or not math.isfinite(metadata["source_timestamp"])):
            raise ValueError("invalid memory source")
        MemoryUpdate.model_validate({"filename": filename, **{key: value for key, value in metadata.items() if key not in {"source", "source_timestamp"}},
                                     "content": content.strip()})
        return {"filename": filename, **metadata, "content": self.redactor(content.strip())}

    def catalog(self, offset=0, limit=20):
        topics = self._path("topics")
        paths = sorted(path.name for path in topics.iterdir() if path.suffix == ".md")
        selected = paths[offset:offset + limit]
        entries = [{key: value for key, value in self.read(name).items()
                    if key in {"filename", "type", "name", "description", "source_timestamp"}} for name in selected]
        next_offset = offset + len(entries) if offset + len(entries) < len(paths) else None
        return {"entries": entries, "next_offset": next_offset}

    def index(self):
        catalog = self.catalog(limit=10)
        if not catalog["entries"]:
            return ""
        lines = ["Project memory directory (historical reference, not current facts or permission):"]
        lines.extend(f"- {item['filename']} [{item['type']}]: {item['description']}" for item in catalog["entries"])
        lines.append("Use list_memories for the directory and read_memory for details. Current instructions win.")
        if catalog["next_offset"] is not None:
            lines.append(f"More entries: list_memories(offset={catalog['next_offset']}).")
        return self.redactor("\n".join(lines))

    def prepare(self, state, key, changes, *, source_ids, read_files, source_timestamp=None, source_times=None):
        source_timestamp = time.time() if source_timestamp is None else source_timestamp
        if type(source_timestamp) not in {int, float} or not math.isfinite(source_timestamp):
            raise ValueError("invalid extraction source time")
        changes = MemoryChanges.model_validate(changes)
        names = [item.filename for item in changes.updates] + changes.deletes
        if len(set(names)) != len(names):
            raise ValueError("memory changes must have unique filenames")
        writes = {}
        for item in changes.updates:
            path = self._path(item.filename, topic=True)
            if path.exists() and item.filename not in read_files:
                raise ValueError("read existing memory before updating it")
            if not set(item.source_event_ids) <= source_ids:
                raise ValueError("memory source reference is not in extraction input")
            observed_at = max(source_times[event_id] for event_id in item.source_event_ids) if source_times is not None else source_timestamp
            if state["forgotten"].get(item.filename, 0) >= observed_at:
                continue
            if path.exists() and self.read(item.filename)["source_timestamp"] > observed_at:
                continue  # An older retried source cannot replace a newer correction.
            metadata = {"type": item.type, "name": self.redactor(item.name),
                        "description": self.redactor(item.description), "source": key,
                        "source_event_ids": item.source_event_ids, "source_timestamp": observed_at}
            text = "---\n" + json.dumps(metadata, ensure_ascii=False, indent=2) + "\n---\n\n" + self.redactor(item.content) + "\n"
            if len(text.encode("utf-8")) > MEMORY_MAX_BYTES:
                raise ValueError("memory topic exceeds size limit")
            writes[item.filename] = text
        deletes = []
        for name in changes.deletes:
            self._path(name, topic=True)
            if name not in read_files:
                raise ValueError("read existing memory before deleting it")
            if self.read(name)["source_timestamp"] <= source_timestamp:
                deletes.append(name)
        state["pending"] = {"key": key, "writes": writes, "deletes": deletes, "source_timestamp": source_timestamp}
        self.save_state(state)  # Stable filenames and bytes, before any file mutation.

    def recover(self, state):
        pending = state["pending"]
        if pending is None:
            return
        if not isinstance(pending, dict) or set(pending) != {"key", "writes", "deletes", "source_timestamp"}:
            raise ValueError("invalid pending memory update")
        for name, text in pending["writes"].items():
            if not isinstance(text, str) or len(text.encode("utf-8")) > MEMORY_MAX_BYTES:
                raise ValueError("invalid pending memory content")
            atomic_replace_bytes(self._path(name, topic=True), text.encode("utf-8"))
        for name in pending["deletes"]:
            self._path(name, topic=True).unlink(missing_ok=True)
        key = pending["key"]
        finished = {**state, "processed_runs": list(state["processed_runs"]), "failures": dict(state["failures"]), "forgotten": dict(state["forgotten"])}
        for name in pending["deletes"]:
            finished["forgotten"][name] = max(finished["forgotten"].get(name, 0), pending["source_timestamp"])
        if key.startswith("request_"):
            finished["requests"] = [item for item in state["requests"] if item["id"] != key]
        elif key not in finished["processed_runs"]:
            finished["processed_runs"].append(key)
        finished["failures"].pop(key, None)
        finished["pending"] = None
        self.save_state(finished)
        state.update(finished)

    def remember(self, text):
        if not text.strip() or len(text) > 4000:
            raise ValueError("remember request must contain 1–4000 characters")
        with self.writer():
            state = self.state()
            self.recover(state)
            state["requests"].append({"id": "request_" + uuid.uuid4().hex, "text": self.redactor(text), "timestamp": time.time()})
            self.save_state(state)

    def forget(self, filename):
        with self.writer():
            state = self.state()
            self.recover(state)  # Never let a pre-existing pending plan resurrect it.
            if not self._path(filename, topic=True).exists():
                raise FileNotFoundError(filename)
            key = "request_" + uuid.uuid4().hex
            state["pending"] = {"key": key, "writes": {}, "deletes": [filename], "source_timestamp": time.time()}
            self.save_state(state)
            self.recover(state)

    def failed(self, state, key, error):
        previous = state["failures"].get(key, {})
        attempts = previous.get("attempts", 0) + 1
        state["failures"][key] = {"attempts": attempts,
                                  "retry_after": time.time() + min(3600, 60 * 2 ** min(attempts - 1, 6)),
                                  "error": self.redactor(str(error))[:1000]}
        self.save_state(state)
