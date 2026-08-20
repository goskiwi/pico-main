"""Single durable fact source for one Pico run."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import FailureInfo, ToolOutcome
from .task_state import STOP_REASON_FINAL_ANSWER_RETURNED

JOURNAL_SCHEMA_VERSION = "run-journal-v3"
CONTEXT_KINDS = frozenset(
    {
        "user_message",
        "assistant_tool_call",
        "tool_result",
        "guidance",
        "assistant_final",
        "compaction",
    }
)
JOURNAL_KINDS = frozenset(
    {
        *CONTEXT_KINDS,
        "run_started",
        "run_resumed",
        "model_requested",
        "turn_metrics",
        "memory_selection",
        "completion_blocked",
        "tool_started",
        "verification_started",
        "verification_result",
        "policy_decided",
        "provider_session_reset",
        "run_stopped",
    }
)


def _clip(text, limit=320):
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 2].rstrip() + " …"


def _unique(values, limit=8, clip_limit=320):
    result = []
    for value in values:
        value = _clip(value, clip_limit)
        if value and value not in result:
            result.append(value)
        if len(result) >= limit:
            break
    return result


@dataclass(frozen=True)
class JournalEntry:
    entry_id: str
    sequence: int
    run_id: str
    task_id: str
    session_id: str
    kind: str
    timestamp: str
    payload: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in JOURNAL_KINDS:
            raise ValueError(f"unsupported Run Journal kind: {self.kind}")
        if self.sequence < 1:
            raise ValueError("Run Journal sequence must be positive")
        if not isinstance(self.payload, dict):
            raise TypeError("Run Journal payload must be an object")

    def to_dict(self):
        return {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "entry_id": self.entry_id,
            "sequence": self.sequence,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "kind": self.kind,
            "timestamp": self.timestamp,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, value):
        expected = {
            "schema_version",
            "entry_id",
            "sequence",
            "run_id",
            "task_id",
            "session_id",
            "kind",
            "timestamp",
            "payload",
        }
        if (
            not isinstance(value, dict)
            or set(value) != expected
            or value.get("schema_version") != JOURNAL_SCHEMA_VERSION
        ):
            raise ValueError("invalid Run Journal entry")
        return cls(
            entry_id=str(value["entry_id"]),
            sequence=int(value["sequence"]),
            run_id=str(value["run_id"]),
            task_id=str(value["task_id"]),
            session_id=str(value["session_id"]),
            kind=str(value["kind"]),
            timestamp=str(value["timestamp"]),
            payload=dict(value["payload"]),
        )

    @property
    def content(self):
        if self.kind in {"user_message", "guidance", "assistant_final"}:
            return str(self.payload.get("content", ""))
        if self.kind == "tool_result":
            outcome = dict(self.payload.get("outcome", {}) or {})
            content = str(outcome.get("content", ""))
            recovery = dict(outcome.get("recovery", {}) or {})
            if recovery:
                guidance = " | ".join(str(item) for item in recovery.get("guidance", []))
                content += (
                    f"\nRuntime recovery: action={recovery.get('action', '')}; "
                    f"{guidance}"
                )
            return content
        if self.kind == "compaction":
            return str(self.payload.get("content", ""))
        return ""

    @property
    def name(self):
        if self.kind == "assistant_tool_call":
            return str(self.payload.get("name", ""))
        if self.kind == "tool_result":
            outcome = dict(self.payload.get("outcome", {}) or {})
            return str(outcome.get("tool_name", self.payload.get("tool_name", "")))
        return ""

    @property
    def args(self):
        return dict(self.payload.get("args", {}) or {})

    @property
    def call_id(self):
        if self.kind == "assistant_tool_call":
            return str(self.payload.get("call_id", ""))
        outcome = dict(self.payload.get("outcome", {}) or {})
        return str(
            self.payload.get("tool_call_id", "")
            or outcome.get("tool_call_id", "")
        )

    @property
    def outcome_status(self):
        outcome = dict(self.payload.get("outcome", {}) or {})
        return str(outcome.get("status", ""))

    @property
    def side_effect_state(self):
        outcome = dict(self.payload.get("outcome", {}) or {})
        return str(outcome.get("side_effect_state", ""))

    @property
    def affected_paths(self):
        outcome = dict(self.payload.get("outcome", {}) or {})
        return tuple(str(item) for item in outcome.get("affected_paths", []))

    @property
    def artifact_id(self):
        outcome = dict(self.payload.get("outcome", {}) or {})
        artifact = dict(outcome.get("artifact", {}) or {})
        return str(artifact.get("artifact_id", ""))

    @property
    def content_tier(self):
        outcome = dict(self.payload.get("outcome", {}) or {})
        return "artifact_reference" if outcome.get("output_truncated") else "inline"

    @property
    def original_size_bytes(self):
        outcome = dict(self.payload.get("outcome", {}) or {})
        artifact = dict(outcome.get("artifact", {}) or {})
        return int(artifact.get("size_bytes", len(self.content.encode("utf-8"))))

    @property
    def summary(self):
        return dict(self.payload.get("summary", {}) or {})

    @property
    def covered_entry_ids(self):
        return tuple(str(item) for item in self.payload.get("covered_entry_ids", []))


@dataclass(frozen=True)
class JournalCursor:
    sequence: int = 0
    entry_id: str = ""

    def to_dict(self):
        return {"sequence": self.sequence, "entry_id": self.entry_id}


@dataclass
class RunProjection:
    run_id: str = ""
    task_id: str = ""
    session_id: str = ""
    user_request: str = ""
    status: str = "running"
    stop_reason: str = ""
    final_answer: str = ""
    run_duration_ms: int = 0
    attempts: int = 0
    tool_steps: int = 0
    last_tool: str = ""
    kind_counts: dict[str, int] = field(default_factory=dict)
    tool_counts: dict[str, int] = field(default_factory=dict)
    outcome_counts: dict[str, int] = field(default_factory=dict)
    policy_counts: dict[str, int] = field(default_factory=dict)
    verification_counts: dict[str, int] = field(default_factory=dict)
    operations: dict[str, dict] = field(default_factory=dict)
    last_cursor: JournalCursor = field(default_factory=JournalCursor)

    def apply(self, entry):
        kind = entry.kind
        payload = dict(entry.payload)
        self.run_id = entry.run_id
        self.task_id = entry.task_id or self.task_id
        self.session_id = entry.session_id or self.session_id
        self.kind_counts[kind] = self.kind_counts.get(kind, 0) + 1
        self.last_cursor = JournalCursor(entry.sequence, entry.entry_id)
        if kind == "user_message" and not self.user_request:
            self.user_request = str(payload.get("content", ""))
        elif kind == "model_requested":
            self.attempts = max(self.attempts, int(payload.get("attempts", 0)))
        elif kind == "tool_started":
            call_id = str(payload.get("tool_call_id", ""))
            if call_id:
                self.operations[call_id] = {"state": "started", **payload}
        elif kind == "tool_result":
            outcome = dict(payload.get("outcome", {}) or {})
            call_id = str(payload.get("tool_call_id", "") or outcome.get("tool_call_id", ""))
            if call_id:
                self.operations[call_id] = {"state": "finished", **payload}
            tool_name = str(outcome.get("tool_name", payload.get("tool_name", "")))
            status = str(outcome.get("status", "unknown"))
            if tool_name and outcome.get("execution_state") != "not_started":
                self.tool_steps += 1
                self.last_tool = tool_name
                self.tool_counts[tool_name] = self.tool_counts.get(tool_name, 0) + 1
            self.outcome_counts[status] = self.outcome_counts.get(status, 0) + 1
        elif kind == "verification_result":
            status = str(payload.get("status", "unknown"))
            self.verification_counts[status] = self.verification_counts.get(status, 0) + 1
        elif kind == "policy_decided":
            decision = "stop" if payload.get("stop") else "continue"
            self.policy_counts[decision] = self.policy_counts.get(decision, 0) + 1
        elif kind == "assistant_final":
            self.status = "completed"
            self.stop_reason = str(payload["stop_reason"])
            self.final_answer = str(payload.get("content", ""))
            self.run_duration_ms = int(payload.get("run_duration_ms", 0))
        elif kind == "run_stopped":
            self.status = "stopped"
            self.stop_reason = str(payload.get("stop_reason", ""))
            self.final_answer = str(payload.get("content", ""))
            self.run_duration_ms = int(payload.get("run_duration_ms", 0))
        return self

    @property
    def terminal(self):
        return self.status in {"completed", "stopped"}

    def task_state(self):
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "user_request": self.user_request,
            "status": self.status,
            "tool_steps": self.tool_steps,
            "attempts": self.attempts,
            "last_tool": self.last_tool,
            "stop_reason": self.stop_reason,
            "final_answer": self.final_answer,
        }

    def summary(self):
        return {
            **self.task_state(),
            "session_id": self.session_id,
            "run_duration_ms": self.run_duration_ms,
            "kind_counts": dict(sorted(self.kind_counts.items())),
            "tool_counts": dict(sorted(self.tool_counts.items())),
            "outcome_counts": dict(sorted(self.outcome_counts.items())),
            "policy_counts": dict(sorted(self.policy_counts.items())),
            "verification_counts": dict(sorted(self.verification_counts.items())),
            "pending_operations": sorted(
                call_id
                for call_id, item in self.operations.items()
                if item.get("state") != "finished"
            ),
            "journal_cursor": self.last_cursor.to_dict(),
        }


def replay_entries(entries):
    projection = RunProjection()
    for entry in entries:
        projection.apply(entry)
    return projection


class RunJournal:
    """Append-only Run facts plus the model-visible context projection."""

    def __init__(self, run_id, task_id, session_id, store, entries=()):
        self.run_id = str(run_id)
        self.task_id = str(task_id)
        self.session_id = str(session_id)
        self.store = store
        self.generation = 1
        self._entries = list(entries)
        compactions = [entry for entry in self._entries if entry.kind == "compaction"]
        self.generation = len(compactions) + 1
        self.reconciled_outcomes = []

    @classmethod
    def restore(cls, run_id, store):
        entries = store.read_entries(run_id)
        if not entries:
            raise ValueError("active Run Journal is missing or empty")
        first = entries[0]
        return cls(first.run_id, first.task_id, first.session_id, store, entries)

    @property
    def entries(self):
        return tuple(self._entries)

    def append(self, kind, payload=None):
        entry = self.store.append_entry(
            self.run_id,
            self.task_id,
            self.session_id,
            kind,
            payload or {},
        )
        self._entries.append(entry)
        return entry

    def append_user(self, content):
        return self.append("user_message", {"content": str(content)})

    def append_tool_call(self, call):
        if self.pending_call_id():
            raise RuntimeError("a tool call is already pending")
        return self.append(
            "assistant_tool_call",
            {"name": call.name, "args": dict(call.args), "call_id": call.call_id},
        )

    def append_tool_result(self, outcome):
        pending = self.pending_call_id()
        if not pending or pending != outcome.tool_call_id:
            raise RuntimeError("tool result must match the pending call")
        return self.append(
            "tool_result",
            {
                "tool_call_id": outcome.tool_call_id,
                "tool_name": outcome.tool_name,
                "workspace_revision": 0,
                "outcome": outcome.to_dict(),
            },
        )

    def append_guidance(self, content):
        self._require_no_pending()
        return self.append("guidance", {"content": str(content)})

    def append_final(self, content, *, run_duration_ms=0):
        self._require_no_pending()
        return self.append(
            "assistant_final",
            {
                "content": str(content),
                "stop_reason": STOP_REASON_FINAL_ANSWER_RETURNED,
                "run_duration_ms": int(run_duration_ms),
            },
        )

    def append_stopped(self, content, stop_reason, *, run_duration_ms=0):
        self._require_no_pending()
        return self.append(
            "run_stopped",
            {
                "content": str(content),
                "stop_reason": str(stop_reason),
                "run_duration_ms": int(run_duration_ms),
            },
        )

    def pending_call_id(self):
        calls = {
            entry.call_id
            for entry in self._entries
            if entry.kind == "assistant_tool_call" and entry.call_id
        }
        completed = {
            entry.call_id
            for entry in self._entries
            if entry.kind == "tool_result" and entry.call_id
        }
        pending = sorted(calls - completed)
        if len(pending) > 1:
            raise RuntimeError("Run Journal contains multiple pending tool calls")
        return pending[0] if pending else ""

    def _require_no_pending(self):
        if self.pending_call_id():
            raise RuntimeError("pending tool call must receive a result first")

    def reconcile_interrupted(self, runtime):
        pending = self.pending_call_id()
        if not pending:
            return ()
        call = next(
            entry
            for entry in reversed(self._entries)
            if entry.kind == "assistant_tool_call" and entry.call_id == pending
        )
        started = next(
            (
                entry
                for entry in reversed(self._entries)
                if entry.kind == "tool_started" and entry.call_id == pending
            ),
            None,
        )
        if started is None:
            detail = "tool call was persisted but never entered execution"
            outcome = ToolOutcome(
                tool_call_id=pending,
                tool_name=call.name,
                status="error",
                execution_state="not_started",
                side_effect_state="none",
                content=detail,
                admission_status="recovered",
                failure=FailureInfo("operation_not_started", "recovery", detail, True),
            )
        else:
            potential = list(started.payload.get("potential_effects", []))
            changed = []
            for effect in potential:
                logical = str(effect.get("path", ""))
                if not logical:
                    continue
                path = Path(logical)
                if not path.is_absolute():
                    path = runtime.workspace.resolve_path(logical)
                before = str(effect.get("before_state", ""))
                after = runtime.workspace.path_state(path)
                if before != after:
                    changed.append(logical)
            effect_scope = str(started.payload.get("effect_scope", "none"))
            unknown = effect_scope in {"workspace", "mixed"} and not potential
            uncertain = bool(changed or unknown)
            detail = "tool execution was interrupted before a durable result"
            outcome = ToolOutcome(
                tool_call_id=pending,
                tool_name=call.name,
                status="partial_success" if uncertain else "error",
                execution_state="failed",
                side_effect_state="partial" if changed else ("unknown" if unknown else "none"),
                content=detail,
                admission_status="recovered",
                failure=FailureInfo(
                    "operation_interrupted", "recovery", detail, not uncertain
                ),
                affected_paths=tuple(changed),
                effect_scope=effect_scope if changed or unknown else "none",
            )
        entry = self.append(
            "tool_result",
            {
                "tool_call_id": outcome.tool_call_id,
                "tool_name": outcome.tool_name,
                "workspace_revision": runtime.workspace.revision,
                "recovered_from_interruption": True,
                "outcome": outcome.to_dict(),
            },
        )
        self.reconciled_outcomes.append((outcome, entry))
        return tuple(self.reconciled_outcomes)

    def context_entries(self):
        calls = {
            entry.call_id
            for entry in self._entries
            if entry.kind == "assistant_tool_call" and entry.call_id
        }
        return tuple(
            entry
            for entry in self._entries
            if entry.kind in CONTEXT_KINDS
            and (entry.kind != "tool_result" or entry.call_id in calls)
        )

    def active_entries(self):
        context = self.context_entries()
        covered = {
            item
            for entry in context
            if entry.kind == "compaction"
            for item in entry.covered_entry_ids
        }
        return tuple(entry for entry in context if entry.entry_id not in covered)

    def compaction_regions(self, *, retain_tokens, token_counter):
        active = list(self.active_entries())
        units = []
        index = 0
        while index < len(active):
            entry = active[index]
            if entry.kind == "assistant_tool_call":
                if index + 1 >= len(active):
                    return {
                        "compact_history": tuple(
                            item for unit in units for item in unit
                        ),
                        "retained_suffix": (),
                        "raw_tail": (entry,),
                        "retained_tokens": 0,
                    }
                result = active[index + 1]
                if result.kind != "tool_result" or result.call_id != entry.call_id:
                    raise RuntimeError("Run Journal tool batch is not contiguous")
                units.append((entry, result))
                index += 2
                continue
            if entry.kind == "tool_result":
                raise RuntimeError("Run Journal contains an orphan tool result")
            units.append((entry,))
            index += 1
        retained_tokens = 0
        retained_units = 0
        limit = max(1, int(retain_tokens))
        for unit in reversed(units):
            text = "\n".join(self._entry_search_text(item) for item in unit)
            unit_tokens = max(1, int(token_counter(text)))
            if retained_units and retained_tokens + unit_tokens > limit:
                break
            retained_tokens += unit_tokens
            retained_units += 1
        cut = max(0, len(units) - retained_units)
        return {
            "compact_history": tuple(item for unit in units[:cut] for item in unit),
            "retained_suffix": tuple(item for unit in units[cut:] for item in unit),
            "raw_tail": (),
            "retained_tokens": retained_tokens,
        }

    def build_structured_summary(self, entries):
        entries = tuple(entries)
        calls = {
            entry.call_id: entry
            for entry in entries
            if entry.kind == "assistant_tool_call"
        }
        goals, completed, facts, files, failures, decisions, questions, next_steps = (
            [],
            [],
            [],
            [],
            [],
            [],
            [],
            [],
        )
        for entry in entries:
            if entry.kind == "compaction":
                goals.extend(entry.summary.get("goal", []))
                completed.extend(entry.summary.get("completed", []))
                facts.extend(entry.summary.get("key_facts", []))
                decisions.extend(entry.summary.get("decisions", []))
                files.extend(entry.summary.get("file_state", []))
                failures.extend(entry.summary.get("tool_failures", []))
                questions.extend(entry.summary.get("open_questions", []))
                next_steps.extend(entry.summary.get("next_steps", []))
            elif entry.kind == "user_message":
                goals.append(entry.content)
            elif entry.kind == "assistant_final":
                decisions.append(entry.content)
            elif entry.kind == "guidance":
                next_steps.append(entry.content)
            elif entry.kind == "tool_result":
                call = calls.get(entry.call_id)
                path = str((call.args or {}).get("path", "")) if call else ""
                label = f"{entry.name}({path})" if path else entry.name
                files.extend([path, *entry.affected_paths])
                if entry.outcome_status == "ok":
                    completed.append(label)
                    if entry.name in {"read_file", "search", "list_files"}:
                        facts.append(f"{label}: {entry.content}")
                else:
                    failures.append(
                        f"{label} [{entry.outcome_status}]: {entry.content}"
                    )
        return {
            "goal": _unique(goals, limit=4, clip_limit=2000),
            "completed": _unique(completed),
            "key_facts": _unique(facts),
            "decisions": _unique(decisions),
            "file_state": _unique(files),
            "tool_failures": _unique(failures),
            "open_questions": _unique(questions),
            "next_steps": _unique(next_steps),
        }

    def commit_compaction(self, summary, covered_entry_ids):
        self._require_no_pending()
        covered = tuple(str(item) for item in covered_entry_ids)
        if not covered or len(set(covered)) != len(covered):
            raise ValueError("compaction must cover a non-empty unique prefix")
        active = self.active_entries()
        if covered != tuple(entry.entry_id for entry in active[: len(covered)]):
            raise ValueError("compaction coverage must be the exact active prefix")
        remaining = active[len(covered) :]
        if remaining and remaining[0].kind == "tool_result":
            raise ValueError("compaction cannot split a tool call/result batch")
        structured = (
            dict(summary)
            if isinstance(summary, dict)
            else {"summary": [_clip(summary, 600)]}
        )
        self.generation += 1
        content = self._render_summary(structured)
        return self.append(
            "compaction",
            {
                "content": content,
                "summary": structured,
                "covered_entry_ids": list(covered),
            },
        )

    @staticmethod
    def _entry_search_text(entry):
        return " ".join(
            [
                entry.kind,
                entry.name,
                entry.content,
                json.dumps(entry.args or {}, sort_keys=True),
                *entry.affected_paths,
            ]
        )

    def render_projection(self, query, exclude_user_content=None):
        del query
        active = self.active_entries()
        selected = list(active)
        if exclude_user_content is not None:
            for index in range(len(selected) - 1, -1, -1):
                entry = selected[index]
                if entry.kind == "user_message" and entry.content == str(
                    exclude_user_content
                ):
                    selected.pop(index)
                    break
        lines = ["Current run journal:"]
        artifact_references = 0
        for entry in selected:
            if entry.kind == "assistant_tool_call":
                lines.append(
                    f"[assistant/tool] {entry.name} "
                    + json.dumps(entry.args or {}, ensure_ascii=False, sort_keys=True)
                )
            elif entry.kind == "tool_result":
                artifact = f" artifact={entry.artifact_id}" if entry.artifact_id else ""
                if entry.artifact_id:
                    artifact_references += 1
                lines.append(
                    f"[tool/{entry.name}/{entry.outcome_status}/{entry.side_effect_state}{artifact}] "
                    f"{entry.content}"
                )
            else:
                lines.append(f"[{entry.kind}] {entry.content}")
        if len(lines) == 1:
            lines.append("- empty")
        return "\n".join(lines), {
            "active_count": len(active),
            "selected_count": len(selected),
            "omitted_count": max(0, len(active) - len(selected)),
            "artifact_references": artifact_references,
        }

    @staticmethod
    def _render_summary(summary):
        labels = (
            ("goal", "Goal"),
            ("completed", "Completed"),
            ("key_facts", "Key facts"),
            ("decisions", "Decisions"),
            ("file_state", "File state"),
            ("tool_failures", "Tool failures"),
            ("open_questions", "Open questions"),
            ("next_steps", "Next steps"),
        )
        lines = ["Runtime Facts:"]
        for key, label in labels:
            values = summary.get(key, [])
            if isinstance(values, str):
                values = [values]
            for value in values:
                limit = 2000 if key == "goal" else 600
                lines.append(f"- {label}: {_clip(value, limit)}")
        semantic = summary.get("summary", [])
        if isinstance(semantic, str):
            semantic = [semantic]
        if semantic:
            lines.extend(["", "LLM Semantic Summary:"])
            lines.extend(f"- {_clip(value, 4000)}" for value in semantic)
        return "\n".join(lines)
