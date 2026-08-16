import json

import pytest

from pico.event_cli import event_main
from pico.run_store import RunStore


def test_event_replay_projects_operations_and_stats(tmp_path, capsys):
    store = RunStore(tmp_path / ".pico" / "runs")
    store.append_event("run_a", "task_a", "run_started", {"user_request": "inspect"})
    store.append_event(
        "run_a",
        "task_a",
        "tool_rejected",
        {
            "tool_call_id": "call_rejected",
            "tool_name": "read_file",
            "outcome": {
                "tool_call_id": "call_rejected",
                "tool_name": "read_file",
                "status": "rejected",
                "execution_state": "not_started",
            },
        },
        correlation_id="call_rejected",
    )
    store.append_event(
        "run_a",
        "task_a",
        "operation_started",
        {"tool_call_id": "call_a", "tool_name": "read_file"},
        correlation_id="call_a",
    )
    store.append_event(
        "run_a",
        "task_a",
        "operation_finished",
        {
            "tool_call_id": "call_a",
            "tool_name": "read_file",
            "outcome": {"tool_call_id": "call_a", "tool_name": "read_file", "status": "ok"},
        },
        correlation_id="call_a",
    )

    summary = store.replay("run_a").summary()
    assert summary["tool_steps"] == 1
    assert summary["tool_counts"] == {"read_file": 1}
    assert summary["outcome_counts"] == {"ok": 1, "rejected": 1}
    assert summary["pending_operations"] == []

    assert event_main(["stats", "run_a", "--cwd", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["event_cursor"]["sequence"] == 4


def test_event_replay_rejects_tampered_hash_chain(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    store.append_event("run_b", "task_b", "run_started", {"user_request": "original"})
    path = store.events_path("run_b")
    path.write_text(path.read_text().replace("original", "tampered"), encoding="utf-8")

    with pytest.raises(ValueError, match="digest"):
        store.read_events("run_b")


def test_event_projection_keeps_interrupted_operation_unknown(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    store.append_event(
        "run_c",
        "task_c",
        "operation_started",
        {"tool_call_id": "call_pending", "tool_name": "patch_file"},
        correlation_id="call_pending",
    )

    receipt = store.operation_receipt("run_c", "call_pending")
    assert receipt["state"] == "started"
    assert store.replay("run_c").summary()["pending_operations"] == ["call_pending"]
