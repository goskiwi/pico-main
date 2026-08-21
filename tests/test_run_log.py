import json

import pytest

from pico.contracts import FailureInfo, ToolCall, ToolOutcome
from pico.run_cli import run_main
from pico.run_log import RunEvent, RunLog
from pico.run_store import RunStore
from pico.task_state import TaskState


def append(store, run_id, kind, payload=None):
    return store.append_event(run_id, "task_a", "session_a", kind, payload or {})


def test_run_log_projects_operations_and_cli_views(tmp_path, capsys):
    store = RunStore(tmp_path / ".pico" / "runs")
    append(store, "run_a", "user_message", {"content": "inspect"})
    append(
        store,
        "run_a",
        "run_started",
        {"task_id": "task_a", "workspace_root": "/workspace"},
    )
    append(
        store,
        "run_a",
        "assistant_tool_call",
        {"name": "read_file", "args": {"path": "README.md"}, "call_id": "call_a"},
    )
    append(
        store,
        "run_a",
        "tool_started",
        {
            "tool_call_id": "call_a",
            "tool_name": "read_file",
            "tool_call_hash": "hash_a",
            "risky": False,
            "effect_scope": "none",
            "potential_effects": [],
        },
    )
    outcome = ToolOutcome(
        tool_call_id="call_a",
        tool_name="read_file",
        status="success",
        execution_state="completed",
        side_effect_state="none",
        content="read",
    )
    append(
        store,
        "run_a",
        "tool_result",
        {
            "tool_call_id": "call_a",
            "tool_name": "read_file",
            "workspace_revision": 0,
            "outcome": outcome.to_dict(),
        },
    )

    summary = store.replay("run_a").summary()
    assert summary["executed_tool_count"] == 1
    assert summary["tool_counts"] == {"read_file": 1}
    assert summary["pending_operations"] == []

    assert run_main(["show", "run_a", "--cwd", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["run_cursor"]["sequence"] == 5

    assert run_main(["events", "run_a", "--cwd", str(tmp_path)]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event["sequence"] for event in events] == [1, 2, 3, 4, 5]


def test_run_lifecycle_markers_cannot_overwrite_the_original_goal(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    original = "repair " + "the original task " * 40
    append(store, "run_goal", "user_message", {"content": original})
    append(
        store,
        "run_goal",
        "run_started",
        {"task_id": "task_a", "workspace_root": "/workspace"},
    )
    append(
        store,
        "run_goal",
        "run_resumed",
        {"task_id": "task_a", "workspace_root": "/workspace"},
    )

    assert store.replay("run_goal").user_request == original


def test_run_log_rejects_complete_malformed_middle_entry(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    append(store, "run_bad", "user_message", {"content": "Inspect"})
    append(
        store,
        "run_bad",
        "run_started",
        {"task_id": "task_a", "workspace_root": "/workspace"},
    )
    path = store.events_path("run_bad")
    with path.open("ab") as handle:
        handle.write(b"{bad json}\n")

    with pytest.raises(ValueError, match="not valid JSON"):
        store.read_events("run_bad")


def test_run_log_rejects_invalid_task_projection(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    append(store, "run_invalid_task", "user_message", {"content": "Inspect"})
    with pytest.raises(ValueError, match="assistant_final requires content"):
        append(
            store,
            "run_invalid_task",
            "assistant_final",
            {
                "content": "",
                "stop_reason": "final_answer_returned",
                "run_duration_ms": 0,
            },
        )


def test_run_log_repairs_only_an_incomplete_tail(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    append(store, "run_tail", "user_message", {"content": "Inspect"})
    append(
        store,
        "run_tail",
        "run_started",
        {"task_id": "task_a", "workspace_root": "/workspace"},
    )
    path = store.events_path("run_tail")
    with path.open("ab") as handle:
        handle.write(b'{"incomplete":')

    events = store.read_events("run_tail")

    assert len(events) == 2
    assert path.read_bytes().endswith(b"\n")


def test_run_log_rejects_tool_started_without_tool_call(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    append(store, "run_pending", "user_message", {"content": "Inspect"})
    with pytest.raises(ValueError, match="tool_started must match"):
        append(
            store,
            "run_pending",
            "tool_started",
            {
                "tool_call_id": "call_pending",
                "tool_name": "patch_file",
                "tool_call_hash": "hash_pending",
                "risky": True,
                "effect_scope": "workspace",
                "potential_effects": [],
            },
        )


def test_run_store_creates_only_run_directory_until_first_entry(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    state = TaskState.create(
        run_id="run_empty", task_id="task_empty", user_request="Inspect"
    )

    run_dir = store.start_run(state)

    assert run_dir.is_dir()
    assert list(run_dir.iterdir()) == []


def test_run_cursor_uses_sequence_and_event_id(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    append(store, "run_cursor", "user_message", {"content": "Inspect"})
    append(
        store,
        "run_cursor",
        "run_started",
        {"task_id": "task_a", "workspace_root": "/workspace"},
    )
    last = append(store, "run_cursor", "model_requested", {"prompt_cache_key": None})

    cursor = store.cursor("run_cursor")

    assert cursor.sequence == 3
    assert cursor.event_id == last.event_id
    assert store.replay("run_cursor").model_request_count == 1


def test_run_log_rejects_mismatched_tool_result_before_persistence(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    log = RunLog("run_protocol", "task", "session", store)
    log.append_user("Inspect")
    log.append_tool_call(ToolCall("read_file", {"path": "README.md"}, "call_expected"))
    wrong = ToolOutcome(
        tool_call_id="call_wrong",
        tool_name="read_file",
        status="rejected",
        execution_state="not_started",
        side_effect_state="none",
        content="rejected",
        failure=FailureInfo("rejected", "admission", "rejected", False),
        rejected_at="policy",
    )

    with pytest.raises(ValueError, match="pending tool call"):
        log.append_tool_result(wrong, workspace_revision=0)
    assert [event.kind for event in log.events] == [
        "user_message",
        "assistant_tool_call",
    ]


def test_run_log_rejects_events_after_terminal(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    log = RunLog("run_terminal", "task", "session", store)
    log.append_user("Inspect")
    log.append_final("Done.")

    with pytest.raises(ValueError, match="after a terminal event"):
        log.append_model_instruction("too late")


def test_run_log_rejects_v5_events():
    value = RunEvent(
        event_id="run:event:000001",
        sequence=1,
        run_id="run",
        task_id="task",
        session_id="session",
        kind="user_message",
        timestamp="2026-01-01T00:00:00+00:00",
        payload={"content": "Inspect"},
    ).to_dict()
    value["schema_version"] = "run-log-v5"

    with pytest.raises(ValueError, match="invalid Run event"):
        RunEvent.from_dict(value)
