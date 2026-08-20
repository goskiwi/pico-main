import json

import pytest

from pico.journal_cli import journal_main
from pico.run_store import RunStore
from pico.task_state import TaskState


def append(store, run_id, kind, payload=None):
    return store.append_entry(run_id, "task_a", "session_a", kind, payload or {})


def test_journal_replay_projects_operations_and_stats(tmp_path, capsys):
    store = RunStore(tmp_path / ".pico" / "runs")
    append(store, "run_a", "user_message", {"content": "inspect"})
    append(store, "run_a", "run_started")
    append(
        store,
        "run_a",
        "tool_started",
        {"tool_call_id": "call_a", "tool_name": "read_file"},
    )
    append(
        store,
        "run_a",
        "tool_result",
        {
            "tool_call_id": "call_a",
            "outcome": {
                "tool_call_id": "call_a",
                "tool_name": "read_file",
                "status": "ok",
                "execution_state": "completed",
            },
        },
    )

    summary = store.replay("run_a").summary()
    assert summary["tool_steps"] == 1
    assert summary["tool_counts"] == {"read_file": 1}
    assert summary["pending_operations"] == []

    assert journal_main(["stats", "run_a", "--cwd", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["journal_cursor"]["sequence"] == 4


def test_run_lifecycle_markers_cannot_overwrite_the_original_goal(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    original = "repair " + "the original task " * 40
    append(store, "run_goal", "user_message", {"content": original})
    append(store, "run_goal", "run_started", {"user_request": "clipped"})
    append(store, "run_goal", "run_resumed", {"user_request": "Continue"})

    assert store.replay("run_goal").user_request == original


def test_journal_rejects_complete_malformed_middle_entry(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    append(store, "run_bad", "run_started")
    path = store.journal_path("run_bad")
    with path.open("ab") as handle:
        handle.write(b"{bad json}\n")

    with pytest.raises(ValueError, match="not valid JSON"):
        store.read_entries("run_bad")


def test_journal_repairs_only_an_incomplete_tail(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    append(store, "run_tail", "run_started")
    path = store.journal_path("run_tail")
    with path.open("ab") as handle:
        handle.write(b'{"incomplete":')

    entries = store.read_entries("run_tail")

    assert len(entries) == 1
    assert path.read_bytes().endswith(b"\n")


def test_journal_projection_keeps_interrupted_operation_pending(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    append(
        store,
        "run_pending",
        "tool_started",
        {"tool_call_id": "call_pending", "tool_name": "patch_file"},
    )

    assert store.replay("run_pending").summary()["pending_operations"] == [
        "call_pending"
    ]


def test_run_store_creates_only_run_directory_until_first_entry(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    state = TaskState.create(
        run_id="run_empty", task_id="task_empty", user_request="Inspect"
    )

    run_dir = store.start_run(state)

    assert run_dir.is_dir()
    assert list(run_dir.iterdir()) == []


def test_journal_cursor_uses_sequence_and_entry_id(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    append(store, "run_cursor", "run_started")
    last = append(store, "run_cursor", "model_requested", {"attempts": 1})

    cursor = store.cursor("run_cursor")

    assert cursor.sequence == 2
    assert cursor.entry_id == last.entry_id
