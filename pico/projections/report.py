"""Terminal report projection from Runtime events and explicit snapshot metadata."""

from ..events import replay_events
from ..evidence import EvidenceLedger

PROJECTED_TASK_FIELDS = (
    "status",
    "tool_steps",
    "attempts",
    "last_tool",
    "stop_reason",
    "final_answer",
    "checkpoint_id",
    "resume_status",
)
SUMMARY_TERMINAL_FIELDS = (
    "run_id",
    "task_id",
    "status",
    "stop_reason",
    "attempts",
    "tool_steps",
    "last_tool",
    "checkpoint_id",
)


def build_run_report(
    *,
    events,
    task_snapshot,
    prompt_metadata,
    project_memory_count,
    redacted_env,
):
    task_snapshot = dict(task_snapshot)
    projection = replay_events(events)
    projected_state = projection.task_state(task_snapshot)
    for field in PROJECTED_TASK_FIELDS:
        if task_snapshot[field] != projected_state[field]:
            raise RuntimeError(f"task state diverged from Runtime events: {field}")
    event_summary = projection.summary()
    for field in SUMMARY_TERMINAL_FIELDS:
        event_summary.pop(field, None)
    return {
        "run_id": projected_state["run_id"],
        "task_id": projected_state["task_id"],
        "status": projected_state["status"],
        "stop_reason": projected_state["stop_reason"],
        "final_answer": projected_state["final_answer"],
        "tool_steps": projected_state["tool_steps"],
        "attempts": projected_state["attempts"],
        "checkpoint_id": projected_state["checkpoint_id"],
        "resume_status": projected_state["resume_status"],
        "prompt_metadata": dict(prompt_metadata),
        "project_memory": {"count": int(project_memory_count)},
        "evidence": EvidenceLedger.from_events(events).to_dict(),
        "event_summary": event_summary,
        "redacted_env": dict(redacted_env),
    }
