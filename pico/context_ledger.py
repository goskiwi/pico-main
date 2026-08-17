"""Append-only task context and its bounded, relevance-ranked projection."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from types import SimpleNamespace

from .contracts import FailureInfo, ToolOutcome, canonical_fingerprint

CONTEXT_LEDGER_SCHEMA = "context-ledger-v3"
INLINE_TOOL_OUTPUT_BYTES = 4500
VISIBLE_KINDS = frozenset(
    {"user", "assistant_tool_call", "tool_result", "guidance", "final", "compaction_summary"}
)


def _clip(text, limit=320):
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 2].rstrip() + " …"


def _tokens(text):
    return set(re.findall(r"[A-Za-z0-9_./-]+|[\u4e00-\u9fff]", str(text).lower()))


def _unique(values, limit=8):
    result = []
    for value in values:
        value = _clip(value)
        if value and value not in result:
            result.append(value)
        if len(result) >= limit:
            break
    return result


@dataclass(frozen=True)
class ContextEntry:
    entry_id: str
    sequence: int
    generation: int
    kind: str
    content: str = ""
    name: str = ""
    args: dict | None = None
    call_id: str = ""
    caused_by: str = ""
    artifact_id: str = ""
    outcome_status: str = ""
    side_effect_state: str = ""
    affected_paths: tuple[str, ...] = ()
    content_tier: str = "inline"
    original_size_bytes: int = 0
    summary: dict = field(default_factory=dict)
    covered_entry_ids: tuple[str, ...] = ()

    def __post_init__(self):
        if self.kind not in VISIBLE_KINDS:
            raise ValueError(f"unsupported context kind: {self.kind}")
        if self.kind in {"assistant_tool_call", "tool_result"} and not self.call_id:
            raise ValueError(f"{self.kind} requires call_id")
        if self.kind == "assistant_tool_call" and (not self.name or not isinstance(self.args, dict)):
            raise ValueError("tool call entry requires name and args")
        if self.kind == "tool_result" and self.content_tier not in {"inline", "artifact_reference"}:
            raise ValueError("invalid tool result content tier")

    def to_dict(self):
        return {
            "schema_version": CONTEXT_LEDGER_SCHEMA,
            "entry_id": self.entry_id,
            "sequence": self.sequence,
            "generation": self.generation,
            "kind": self.kind,
            "content": self.content,
            "name": self.name,
            "args": dict(self.args or {}),
            "call_id": self.call_id,
            "caused_by": self.caused_by,
            "artifact_id": self.artifact_id,
            "outcome_status": self.outcome_status,
            "side_effect_state": self.side_effect_state,
            "affected_paths": list(self.affected_paths),
            "content_tier": self.content_tier,
            "original_size_bytes": self.original_size_bytes,
            "summary": dict(self.summary),
            "covered_entry_ids": list(self.covered_entry_ids),
        }

    @classmethod
    def from_dict(cls, value):
        expected = {
            "schema_version", "entry_id", "sequence", "generation", "kind", "content", "name", "args",
            "call_id", "caused_by", "artifact_id", "outcome_status", "side_effect_state", "affected_paths",
            "content_tier", "original_size_bytes", "summary", "covered_entry_ids",
        }
        if not isinstance(value, dict) or set(value) != expected or value.get("schema_version") != CONTEXT_LEDGER_SCHEMA:
            raise ValueError("invalid context ledger entry")
        return cls(
            entry_id=str(value["entry_id"]),
            sequence=int(value["sequence"]),
            generation=int(value["generation"]),
            kind=str(value["kind"]),
            content=str(value["content"]),
            name=str(value["name"]),
            args=dict(value["args"]),
            call_id=str(value["call_id"]),
            caused_by=str(value["caused_by"]),
            artifact_id=str(value["artifact_id"]),
            outcome_status=str(value["outcome_status"]),
            side_effect_state=str(value["side_effect_state"]),
            affected_paths=tuple(str(item) for item in value["affected_paths"]),
            content_tier=str(value["content_tier"]),
            original_size_bytes=int(value["original_size_bytes"]),
            summary=dict(value["summary"]),
            covered_entry_ids=tuple(str(item) for item in value["covered_entry_ids"]),
        )


class ContextLedger:
    """Source of truth for one run; prompts are bounded projections, not copies."""

    def __init__(self, run_id, run_store):
        self.run_id = str(run_id)
        self.run_store = run_store
        self.generation = 1
        self._entries: list[ContextEntry] = []
        self.reconciled_outcomes: list[ToolOutcome] = []

    @classmethod
    def restore(cls, run_id, run_store):
        ledger = cls(run_id, run_store)
        path = run_store.context_path(run_id)
        if not path.exists():
            raise ValueError("checkpoint context ledger is missing")
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if set(record) != {"run_id", "record_type", "payload"}:
                raise ValueError("invalid context ledger record")
            if record["run_id"] != str(run_id) or record["record_type"] != "context_entry":
                raise ValueError("context ledger record belongs to another run")
            entry = ContextEntry.from_dict(record["payload"])
            expected_sequence = len(ledger._entries) + 1
            if entry.sequence != expected_sequence or entry.entry_id != f"{run_id}:ctx:{expected_sequence:06d}":
                raise ValueError("context ledger sequence is not contiguous")
            ledger._entries.append(entry)
        if not ledger._entries:
            raise ValueError("checkpoint context ledger is empty")
        ledger.generation = ledger._entries[-1].generation
        pending = ledger.pending_call_id()
        if pending:
            receipt = run_store.operation_receipt(run_id, pending)
            outcome_data = dict((receipt or {}).get("outcome", {}) or {})
            if (receipt or {}).get("state") == "finished" and outcome_data:
                restored = SimpleNamespace(
                    tool_call_id=pending,
                    tool_name=str(outcome_data.get("tool_name", "unknown")),
                    status=str(outcome_data.get("status", "error")),
                    side_effect_state=str(outcome_data.get("side_effect_state", "unknown")),
                    content=str(outcome_data.get("content", "operation recovered from journal")),
                    affected_paths=tuple(outcome_data.get("affected_paths", [])),
                    artifact_id=str(outcome_data.get("artifact_id", "")),
                    recovery=None,
                )
            else:
                source = next(entry for entry in reversed(ledger._entries) if entry.call_id == pending)
                affected = (str(source.args.get("path")),) if source.args.get("path") else ()
                detail = "operation was interrupted without a terminal receipt; do not replay it blindly"
                restored = ToolOutcome(
                    tool_call_id=pending,
                    tool_name=source.name,
                    status="partial_success",
                    execution_state="failed",
                    side_effect_state="unknown",
                    content=detail,
                    call_fingerprint=canonical_fingerprint(source.name, source.args or {}),
                    admission={"status": "recovered", "stages": []},
                    failure=FailureInfo("operation_interrupted", "recovery", detail, False),
                    affected_paths=affected,
                    metadata={
                        "effect_scope": "workspace" if affected else "unknown",
                        "recovered_from_interruption": True,
                    },
                )
                ledger.reconciled_outcomes.append(restored)
            ledger.append_tool_result(restored)
        return ledger

    @property
    def entries(self):
        return tuple(self._entries)

    def _append(self, kind, **values):
        sequence = len(self._entries) + 1
        entry = ContextEntry(
            entry_id=f"{self.run_id}:ctx:{sequence:06d}",
            sequence=sequence,
            generation=self.generation,
            kind=kind,
            **values,
        )
        self._entries.append(entry)
        self.run_store.append_context(self.run_id, entry.to_dict())
        return entry

    def append_user(self, content):
        return self._append("user", content=str(content))

    def append_tool_call(self, call):
        if self.pending_call_id():
            raise RuntimeError("a tool call is already pending")
        return self._append("assistant_tool_call", name=call.name, args=call.args, call_id=call.call_id)

    @staticmethod
    def _tool_projection(outcome):
        content = str(outcome.content)
        visible_size = len(content.encode("utf-8"))
        original_size = int((outcome.artifact or {}).get("size_bytes", visible_size))
        if visible_size <= INLINE_TOOL_OUTPUT_BYTES:
            return content, "inline", original_size
        head = content[:320].rstrip()
        tail = content[-1100:].lstrip()
        preview = f"{head}\n... [bounded preview] ...\n{tail}"
        reference = (
            f"Full audit output: artifact={outcome.artifact_id}"
            if outcome.artifact_id
            else "Full output stored outside prompt"
        )
        return (
            f"{preview}\n[{reference}; bytes={original_size}]",
            "artifact_reference",
            original_size,
        )

    def append_tool_result(self, outcome):
        pending = self.pending_call_id()
        if not pending or pending != outcome.tool_call_id:
            raise RuntimeError("tool result must match the pending call")
        source = next(entry for entry in reversed(self._entries) if entry.call_id == pending)
        content, content_tier, original_size = self._tool_projection(outcome)
        if outcome.recovery is not None:
            recovery = outcome.recovery
            guidance = " | ".join(recovery.guidance)
            content += (
                f"\nRuntime recovery: action={recovery.action}; "
                f"retryability={recovery.retryability}; {guidance}"
            )
        return self._append(
            "tool_result",
            content=content,
            name=outcome.tool_name,
            call_id=pending,
            caused_by=source.entry_id,
            artifact_id=outcome.artifact_id,
            outcome_status=outcome.status,
            side_effect_state=outcome.side_effect_state,
            affected_paths=outcome.affected_paths,
            content_tier=content_tier,
            original_size_bytes=original_size,
        )

    def append_guidance(self, content):
        self._require_no_pending()
        return self._append("guidance", content=str(content))

    def append_final(self, content):
        self._require_no_pending()
        return self._append("final", content=str(content))

    def pending_call_id(self):
        completed = {entry.call_id for entry in self._entries if entry.kind == "tool_result"}
        pending = [
            entry.call_id
            for entry in self._entries
            if entry.kind == "assistant_tool_call" and entry.call_id not in completed
        ]
        if len(pending) > 1:
            raise RuntimeError("context ledger contains multiple pending calls")
        return pending[0] if pending else ""

    def _require_no_pending(self):
        if self.pending_call_id():
            raise RuntimeError("pending tool call must receive a result first")

    def build_structured_summary(self, entries):
        entries = tuple(entries)
        calls = {entry.call_id: entry for entry in entries if entry.kind == "assistant_tool_call"}
        completed, facts, files, failures, decisions, questions, next_steps = [], [], [], [], [], [], []
        for entry in entries:
            if entry.kind == "compaction_summary":
                completed.extend(entry.summary.get("completed", []))
                facts.extend(entry.summary.get("key_facts", []))
                decisions.extend(entry.summary.get("decisions", []))
                files.extend(entry.summary.get("file_state", []))
                failures.extend(entry.summary.get("tool_failures", []))
                questions.extend(entry.summary.get("open_questions", []))
                next_steps.extend(entry.summary.get("next_steps", []))
            elif entry.kind == "user":
                questions.append(entry.content)
            elif entry.kind == "final":
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
                    failures.append(f"{label} [{entry.outcome_status}]: {entry.content}")
        return {
            "completed": _unique(completed),
            "key_facts": _unique(facts),
            "decisions": _unique(decisions),
            "file_state": _unique(files),
            "tool_failures": _unique(failures),
            "open_questions": _unique(questions),
            "next_steps": _unique(next_steps),
        }

    def active_digest(self):
        """Stable optimistic-lock token for transactional compaction."""
        payload = [entry.to_dict() for entry in self.active_entries()]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def compaction_regions(self, retain_units=3):
        """Split active context without ever bisecting a tool call/result pair."""
        active = list(self.active_entries())
        units = []
        index = 0
        while index < len(active):
            entry = active[index]
            if entry.kind == "assistant_tool_call":
                if index + 1 >= len(active):
                    return {"compact_history": tuple(item for unit in units[:-retain_units] for item in unit),
                            "retained_suffix": tuple(item for unit in units[-retain_units:] for item in unit),
                            "raw_tail": (entry,)}
                result = active[index + 1]
                if result.kind != "tool_result" or result.call_id != entry.call_id:
                    raise RuntimeError("context ledger tool batch is not contiguous")
                units.append((entry, result))
                index += 2
                continue
            if entry.kind == "tool_result":
                raise RuntimeError("context ledger contains an orphan tool result")
            units.append((entry,))
            index += 1
        cut = max(0, len(units) - max(1, int(retain_units)))
        return {
            "compact_history": tuple(item for unit in units[:cut] for item in unit),
            "retained_suffix": tuple(item for unit in units[cut:] for item in unit),
            "raw_tail": (),
        }

    def commit_compaction(self, summary, covered_entry_ids, *, expected_generation, expected_active_digest):
        """Atomically publish a summary if the source context has not changed."""
        self._require_no_pending()
        if self.generation != int(expected_generation) or self.active_digest() != str(expected_active_digest):
            raise RuntimeError("context changed while compaction was being prepared")
        covered = tuple(str(item) for item in covered_entry_ids)
        if not covered or len(set(covered)) != len(covered):
            raise ValueError("compaction must cover a non-empty unique prefix")
        active = self.active_entries()
        if covered != tuple(entry.entry_id for entry in active[: len(covered)]):
            raise ValueError("compaction coverage must be the exact active prefix")
        remaining = active[len(covered) :]
        if remaining and remaining[0].kind == "tool_result":
            raise ValueError("compaction cannot split a tool call/result batch")
        structured = dict(summary) if isinstance(summary, dict) else {"summary": [_clip(summary, 600)]}
        self.generation += 1
        return self._append(
            "compaction_summary",
            content=self._render_summary(structured),
            summary=structured,
            covered_entry_ids=covered,
        )

    def active_entries(self):
        covered = {
            item
            for entry in self._entries
            if entry.kind == "compaction_summary"
            for item in entry.covered_entry_ids
        }
        return tuple(entry for entry in self._entries if entry.entry_id not in covered)

    @staticmethod
    def _entry_search_text(entry):
        return " ".join(
            [entry.kind, entry.name, entry.content, json.dumps(entry.args or {}, sort_keys=True), *entry.affected_paths]
        )

    def ranked_entries(self, query, recent_limit=6, relevant_limit=6):
        active = list(self.active_entries())
        if len(active) <= recent_limit:
            return tuple(active)
        query_tokens = _tokens(query)
        selected_ids = {entry.entry_id for entry in active[-recent_limit:]}
        scored = []
        for entry in active[:-recent_limit]:
            overlap = len(query_tokens & _tokens(self._entry_search_text(entry)))
            kind_weight = 4 if entry.kind == "compaction_summary" else 2 if entry.kind in {"tool_result", "guidance"} else 1
            failure_weight = 3 if entry.outcome_status and entry.outcome_status != "ok" else 0
            score = overlap * 10 + kind_weight + failure_weight
            if overlap or entry.kind == "compaction_summary" or failure_weight:
                scored.append((score, entry.sequence, entry))
        for _, _, entry in sorted(scored, reverse=True)[:relevant_limit]:
            selected_ids.add(entry.entry_id)
            if entry.call_id:
                selected_ids.update(item.entry_id for item in active if item.call_id == entry.call_id)
        return tuple(entry for entry in active if entry.entry_id in selected_ids)

    @staticmethod
    def _render_summary(summary):
        labels = {
            "completed": "Completed",
            "key_facts": "Key facts",
            "decisions": "Decisions",
            "file_state": "File state",
            "tool_failures": "Tool failures",
            "open_questions": "Open questions",
            "next_steps": "Next steps",
            "summary": "Summary",
        }
        lines = ["Structured context summary:"]
        for key, label in labels.items():
            values = list(summary.get(key, []))
            if values:
                lines.append(f"- {label}: " + " | ".join(str(value) for value in values))
        return "\n".join(lines)

    def render_projection(self, query, exclude_user_content=""):
        ranked = self.ranked_entries(query)
        selected = tuple(
            entry
            for entry in ranked
            if not (entry.kind == "user" and entry.content == str(exclude_user_content))
        )
        lines = ["Task context ledger:"]
        for entry in selected:
            if entry.kind == "assistant_tool_call":
                lines.append(f"[assistant/tool:{entry.name}#{entry.call_id}] {entry.args}")
            elif entry.kind == "tool_result":
                suffix = f" artifact={entry.artifact_id}" if entry.artifact_id else ""
                lines.append(f"[tool/result:{entry.name}#{entry.call_id}{suffix}] {entry.content}")
            else:
                lines.append(f"[{entry.kind}] {entry.content}")
        active_count = len(self.active_entries())
        return "\n".join(lines), {
            "active_count": active_count,
            "selected_count": len(selected),
            "omitted_count": active_count - len(selected),
            "current_request_duplicate_avoided": len(ranked) - len(selected),
            "inline_tool_results": sum(entry.kind == "tool_result" and entry.content_tier == "inline" for entry in selected),
            "artifact_references": sum(entry.kind == "tool_result" and entry.content_tier == "artifact_reference" for entry in selected),
        }

    def render(self):
        return self.render_projection("")[0]
