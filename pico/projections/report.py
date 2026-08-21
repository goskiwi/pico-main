"""Terminal report projected on demand from a Run Log."""

from ..evidence import RunEvidence
from ..run_log import replay_events


def build_run_report(
    *,
    events,
):
    projection = replay_events(events)
    projected_state = projection.task_state()
    run_summary = projection.summary()
    persisted_prompt_metadata = next(
        (
            dict(entry.payload.get("prompt_metadata", {}) or {})
            for entry in reversed(events)
            if entry.kind == "turn_metrics"
            and dict(entry.payload.get("prompt_metadata", {}) or {}).get("sections")
        ),
        {},
    )
    completion_metadata = next(
        (
            dict(entry.payload.get("completion_metadata", {}) or {})
            for entry in reversed(events)
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
        "working_state": projected_state["working_state"],
        "executed_tool_count": projected_state["executed_tool_count"],
        "model_request_count": projected_state["model_request_count"],
        "prompt_metadata": persisted_prompt_metadata,
        "completion_metadata": completion_metadata,
        "evidence": RunEvidence.from_events(events).to_dict(),
        "run_summary": run_summary,
    }
