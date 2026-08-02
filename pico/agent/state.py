"""Mutable per-task runtime state helpers.

These helpers operate on a Pico-like object instead of importing ``Pico`` so
the state rules stay reusable without creating an import cycle with runtime.
"""

import json
import re

from .. import memory as memorylib
from ..workspace import clip


_DEDUPLICATED_READ_ONLY_TOOLS = frozenset(
    {"read_file", "list_files", "search", "query_repo_map"}
)


def update_memory_after_tool(agent, name, args, result):
    """Persist only the file facts that are useful on the next model turn."""
    if not agent.feature_enabled("memory"):
        return
    if name == "read_file":
        skill_paths = {skill.path for skill in agent.skills}
        file_args = [
            (index, item)
            for index, item in enumerate(args.get("files", []))
            if (
                isinstance(item, dict)
                and str(item.get("path", "")).strip()
                and agent.memory.canonical_path(item["path"]) not in skill_paths
            )
        ]
        sections = read_file_result_sections(result)
        for index, item in file_args:
            path = str(item["path"])
            canonical_path = agent.memory.canonical_path(path)
            agent.memory.remember_file(canonical_path)
            summary = memorylib.summarize_read_result(
                sections[index] if index < len(sections) else result
            )
            agent.memory.set_file_summary(canonical_path, summary)
    elif name in {"write_file", "patch_file"}:
        path = args.get("path")
        if not path:
            return
        canonical_path = agent.memory.canonical_path(path)
        agent.memory.remember_file(canonical_path)
        agent.memory.invalidate_file_summary(canonical_path)


def read_file_result_sections(result):
    """Extract one batch-read result so each file receives its own summary."""
    text = str(result)
    header = re.compile(
        r"^=== read_file metadata: .+; header and line numbers are not file content ===$",
        flags=re.MULTILINE,
    )
    matches = list(header.finditer(text))
    return [
        text[
            match.end() : matches[index + 1].start()
            if index + 1 < len(matches)
            else len(text)
        ].strip()
        for index, match in enumerate(matches)
    ]


def update_working_state(agent, **updates):
    if not agent.feature_enabled("memory"):
        return
    agent.memory.update_working_state(**updates)
    agent.session["memory"] = agent.memory.to_dict()
    agent.session_path = agent.session_store.save(agent.session)


def mark_work_started(agent, user_message):
    agent._read_only_tool_signatures.clear()
    agent._read_only_tool_evidence.clear()
    agent._context_checkpoint = None
    update_working_state(
        agent,
        goal=user_message,
        current_subtask="building prompt and asking model",
        next_action="parse the model response",
        last_error="",
    )


def mark_tool_planned(agent, name):
    update_working_state(
        agent,
        current_subtask=f"running tool {name}",
        next_action="inspect the tool result",
    )


def read_only_tool_signature(name, args):
    return json.dumps(
        [str(name or ""), args or {}],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def is_duplicate_read_only_tool(agent, name, args):
    if name not in _DEDUPLICATED_READ_ONLY_TOOLS:
        return False
    return read_only_tool_signature(name, args) in agent._read_only_tool_signatures


def cached_read_only_evidence(agent, name, args):
    signature = read_only_tool_signature(name, args)
    return dict(agent._read_only_tool_evidence.get(signature, {}))


def cache_read_only_evidence(agent, name, args, result, *, result_ref="", node_id=""):
    if name not in _DEDUPLICATED_READ_ONLY_TOOLS:
        return
    signature = read_only_tool_signature(name, args)
    agent._read_only_tool_signatures.add(signature)
    agent._read_only_tool_evidence.setdefault(
        signature,
        {
            "result": str(result or ""),
            "result_ref": str(result_ref or ""),
            "node_id": str(node_id or ""),
        },
    )


def mark_tool_finished(agent, name, args, metadata, result):
    del args
    if metadata.get("workspace_changed"):
        agent._read_only_tool_signatures.clear()
        agent._read_only_tool_evidence.clear()
    status = str(metadata.get("tool_status", "")).strip() or "ok"
    error_code = str(metadata.get("tool_error_code", "")).strip()
    if status in {"error", "rejected", "partial_success"}:
        update_working_state(
            agent,
            current_subtask=f"handled tool {name} with status {status}",
            next_action="recover from the tool result",
            last_error=clip(f"{name} {status}: {error_code or result}", 240),
        )
        return
    update_working_state(
        agent,
        current_subtask=f"processed tool {name}",
        next_action="continue reasoning from the tool result",
        last_error="",
    )


def mark_retry_needed(agent, notice):
    update_working_state(
        agent,
        current_subtask="recovering from a rejected model action",
        next_action="ask the model for one valid function call",
        last_error=clip(notice, 240),
    )


def mark_work_finished(agent, final, stopped=False):
    update_working_state(
        agent,
        current_subtask="stopped" if stopped else "completed",
        next_action="-",
        last_error=clip(final, 240) if stopped else "",
    )
