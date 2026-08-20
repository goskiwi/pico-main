"""Terminal report projected on demand from a Run Journal."""

from ..evidence import EvidenceLedger
from ..run_journal import replay_entries


def build_run_report(
    *,
    entries,
    prompt_metadata,
    project_memory_count,
    redacted_env,
):
    projection = replay_entries(entries)
    projected_state = projection.task_state()
    journal_summary = projection.summary()
    persisted_prompt_metadata = next(
        (
            dict(entry.payload.get("prompt_metadata", {}) or {})
            for entry in reversed(entries)
            if entry.kind == "turn_metrics"
        ),
        {},
    )
    return {
        "run_id": projected_state["run_id"],
        "task_id": projected_state["task_id"],
        "status": projected_state["status"],
        "stop_reason": projected_state["stop_reason"],
        "final_answer": projected_state["final_answer"],
        "tool_steps": projected_state["tool_steps"],
        "attempts": projected_state["attempts"],
        "prompt_metadata": dict(prompt_metadata or persisted_prompt_metadata),
        "project_memory": {"count": int(project_memory_count)},
        "evidence": EvidenceLedger.from_entries(entries).to_dict(),
        "journal_summary": journal_summary,
        "redacted_env": dict(redacted_env),
    }
