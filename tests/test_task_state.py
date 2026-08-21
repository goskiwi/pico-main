import pytest

from pico.contracts import ToolOutcome
from pico.features.memory import WorkingState
from pico.run_log import RunEvent, replay_events
from pico.task_state import TaskState


def event(sequence, kind, payload=None):
    return RunEvent(
        event_id=f"run:event:{sequence:06d}",
        sequence=sequence,
        run_id="run_running",
        task_id="task_running",
        session_id="session",
        kind=kind,
        timestamp="2026-01-01T00:00:00+00:00",
        payload=payload or {},
    )


def working_state(goal="Inspect"):
    return WorkingState(goal=goal).to_dict()


def test_task_state_accepts_explicit_consistent_states():
    running = TaskState.create("task_running", "Inspect", run_id="run_running")
    running.apply_event(event(1, "model_requested", {"prompt_cache_key": None}))
    outcome = ToolOutcome(
        tool_call_id="call_read",
        tool_name="read_file",
        status="success",
        execution_state="completed",
        side_effect_state="none",
        content="read",
    )
    running.apply_event(
        event(
            2,
            "tool_result",
            {
                "tool_call_id": "call_read",
                "tool_name": "read_file",
                "workspace_revision": 0,
                "outcome": outcome.to_dict(),
            },
        )
    )

    assert running.status == "running"
    assert running.model_request_count == 1
    assert running.executed_tool_count == 1
    assert running.last_executed_tool == "read_file"

    running.apply_event(
        event(
            3,
            "assistant_final",
            {
                "content": "Done.",
                "stop_reason": "final_answer_returned",
                "run_duration_ms": 0,
            },
        )
    )

    assert running.status == "completed"
    assert running.stop_reason == "final_answer_returned"
    assert running.final_answer == "Done."


def test_live_task_state_matches_run_log_replay():
    outcome = ToolOutcome(
        tool_call_id="call_read",
        tool_name="read_file",
        status="success",
        execution_state="completed",
        side_effect_state="none",
        content="read",
    )
    events = [
        event(1, "user_message", {"content": "Inspect"}),
        event(2, "model_requested", {"prompt_cache_key": None}),
        event(
            3,
            "assistant_tool_call",
            {"name": "read_file", "args": {"path": "README.md"}, "call_id": "call_read"},
        ),
        event(
            4,
            "tool_started",
            {
                "tool_call_id": "call_read",
                "tool_name": "read_file",
                "tool_call_hash": "hash_read",
                "risky": False,
                "effect_scope": "none",
                "potential_effects": [],
            },
        ),
        event(
            5,
            "tool_result",
            {
                "tool_call_id": "call_read",
                "tool_name": "read_file",
                "workspace_revision": 0,
                "outcome": outcome.to_dict(),
            },
        ),
        event(
            6,
            "assistant_final",
            {
                "content": "Done.",
                "stop_reason": "final_answer_returned",
                "run_duration_ms": 0,
            },
        ),
    ]
    live = TaskState.create("task_running", "Inspect", run_id="run_running")

    for item in events:
        live.apply_event(item)

    assert live.to_dict() == replay_events(events).task_state()


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (
            {
                "run_id": "run",
                "task_id": "task",
                "working_state": working_state(),
                "status": "running",
                "stop_reason": "model_error",
            },
            "running task cannot have stop_reason",
        ),
        (
            {
                "run_id": "run",
                "task_id": "task",
                "working_state": working_state(),
                "status": "completed",
                "stop_reason": "final_answer_returned",
                "final_answer": "",
            },
            "completed task requires final_answer",
        ),
        (
            {
                "run_id": "run",
                "task_id": "task",
                "working_state": working_state(),
                "executed_tool_count": 1,
                "last_executed_tool": "",
            },
            "executed tools require last_executed_tool",
        ),
        (
            {
                "run_id": "run",
                "task_id": "task",
                "working_state": working_state(),
                "model_request_count": -1,
            },
            "model_request_count cannot be negative",
        ),
    ],
)
def test_task_state_rejects_inconsistent_fields(data, message):
    with pytest.raises(ValueError, match=message):
        TaskState.from_dict(data)


def test_task_state_rejects_legacy_user_request_shape():
    with pytest.raises(ValueError, match="working_state.goal"):
        TaskState.from_dict(
            {
                "run_id": "run",
                "task_id": "task",
                "user_request": "Inspect",
            }
        )
