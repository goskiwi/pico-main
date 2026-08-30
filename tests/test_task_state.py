from dataclasses import FrozenInstanceError

import pytest

from pico.contracts import ToolOutcome
from pico.delivery import FinalDiffDescriptor
from pico.features.memory import WorkingState
from pico.run_log import RunEvent, replay_events
from pico.task_state import TaskContract, TaskLifecycle, TaskState

READ_TASK = {
    "task_kind": "read_only",
    "requires_workspace_change": False,
    "requires_verification": False,
}
NO_CHANGE_TASK = {
    "task_kind": "modify",
    "requires_workspace_change": False,
    "requires_verification": False,
}
MODIFY_TASK = {
    "task_kind": "modify",
    "requires_workspace_change": True,
    "requires_verification": False,
}


def contract(goal="Inspect"):
    return TaskContract(goal=goal, **READ_TASK)


def event(sequence, kind, payload=None):
    return RunEvent(
        event_id=f"run:event:{sequence:06d}",
        sequence=sequence,
        run_id="run",
        task_id="task",
        session_id="session",
        kind=kind,
        timestamp="2026-01-01T00:00:00+00:00",
        payload=payload or {},
    )


def test_task_state_separates_contract_incremental_working_and_lifecycle():
    task = TaskState.create(contract())
    update = {
        "add_constraints": ["Keep the API"],
        "remove_constraints": [],
        "add_decisions": ["Fix refresh"],
        "remove_decisions": [],
        "add_next_steps": ["Add a test"],
        "remove_next_steps": [],
    }
    task.apply_event(
        event(
            1,
            "assistant_tool_call",
            {"name": "update_working_state", "args": update, "call_id": "state"},
        )
    )
    outcome = ToolOutcome(
        "state",
        "update_working_state",
        "success",
        "completed",
        "none",
        "accepted",
    )
    task.apply_event(
        event(
            2,
            "tool_result",
            {
                "outcome": outcome.to_dict(),
            },
        )
    )
    task.apply_event(
        event(
            3,
            "assistant_final",
            {
                "content": "Done.",
                "stop_reason": "final_answer_returned",
                "run_duration_ms": 1,
                "final_diff": FinalDiffDescriptor().to_dict(),
            },
        )
    )

    assert task.contract.goal == "Inspect"
    assert task.working.constraints == ("Keep the API",)
    assert task.working.decisions == ("Fix refresh",)
    assert task.lifecycle.status == "completed"
    assert task.lifecycle.final_answer == "Done."


def test_live_projection_matches_replay_and_owns_metrics():
    read = ToolOutcome(
        "read",
        "read_file",
        "success",
        "completed",
        "none",
        "read",
    )
    events = [
        event(1, "user_message", {"contract": contract().to_dict()}),
        event(2, "model_requested", {"prompt_cache_key": None}),
        event(
            3,
            "assistant_tool_call",
            {"name": "read_file", "args": {"path": "README.md"}, "call_id": "read"},
        ),
        event(
            4,
            "tool_started",
            {
                "tool_call_id": "read",
                "tool_name": "read_file",
                "risky": False,
                "effect_scope": "none",
                "potential_effects": [],
            },
        ),
        event(
            5,
            "tool_result",
            {
                "outcome": read.to_dict(),
            },
        ),
        event(
            6,
            "assistant_final",
            {
                "content": "Done.",
                "stop_reason": "final_answer_returned",
                "run_duration_ms": 10,
                "final_diff": FinalDiffDescriptor().to_dict(),
            },
        ),
    ]
    projection = replay_events(events)

    assert projection.task.contract == contract()
    assert projection.task.lifecycle.status == "completed"
    assert projection.metrics.model_request_count == 1
    assert projection.metrics.executed_tool_count == 1
    assert projection.pending_call_id is None
    assert projection.final_diff == FinalDiffDescriptor()


def test_task_contract_rejects_inconsistent_requirements():
    with pytest.raises(ValueError, match="read-only"):
        TaskContract("Inspect", "read_only", True, False)
    with pytest.raises(ValueError, match="invalid task kind"):
        TaskContract("Inspect", "missing", False, False)


def test_task_contract_is_immutable_after_validation():
    task_contract = contract()

    with pytest.raises(FrozenInstanceError):
        task_contract.goal = "Changed"  # type: ignore[misc]


def test_replay_events_validates_protocol_before_reducing():
    orphan_call = event(
        1,
        "assistant_tool_call",
        {"name": "read_file", "args": {"path": "README.md"}, "call_id": "read"},
    )

    with pytest.raises(ValueError, match="must begin with user_message"):
        replay_events([orphan_call])


@pytest.mark.parametrize(
    "value, message",
    [
        ({"goal": 123}, "goal must be a string"),
        ({"task_kind": 123}, "task_kind must be a string"),
        ({"allowed_write_paths": [123]}, "entries must be strings"),
    ],
)
def test_task_contract_rejects_non_text_schema_values(value, message):
    payload = contract().to_dict()
    payload.update(value)

    with pytest.raises(TypeError, match=message):
        TaskContract.from_dict(payload)


def test_working_state_no_longer_owns_goal_but_keeps_incremental_protocol():
    with pytest.raises(TypeError):
        WorkingState(goal="duplicated")
    state = WorkingState().apply_update({"add_next_steps": ["Inspect logs"]})
    assert state.next_steps == ("Inspect logs",)


def test_task_contract_rejects_old_untyped_shape():
    with pytest.raises(ValueError, match="invalid task contract fields"):
        TaskContract.from_dict({"goal": "old"})


def test_lifecycle_rejects_completed_state_without_answer():
    with pytest.raises(ValueError, match="requires final_answer"):
        TaskLifecycle("completed", "final_answer_returned", "").validate()
