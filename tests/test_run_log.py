import json

import pytest

from pico.contracts import FailureInfo, ToolCall, ToolOutcome
from pico.delivery import FinalDiffDescriptor
from pico.run_cli import run_main
from pico.run_log import RunEvent, RunLog
from pico.run_store import RunStore
from pico.task_state import TaskContract

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


def append(store, run_id, kind, payload=None):
    payload = dict(payload or {})
    if kind == "user_message":
        payload = {
            "contract": TaskContract(payload.pop("content"), **READ_TASK).to_dict()
        }
    return store.append_event(run_id, "task", "session", kind, payload)


def read_outcome(call_id="read"):
    return ToolOutcome(
        call_id,
        "read_file",
        "success",
        "completed",
        "none",
        "read",
    )


def test_run_log_projects_metrics_and_cli_views(tmp_path, capsys):
    store = RunStore(tmp_path / ".pico/runs")
    append(store, "run", "user_message", {"content": "inspect"})
    append(
        store,
        "run",
        "assistant_tool_call",
        {"name": "read_file", "args": {"path": "README.md"}, "call_id": "read"},
    )
    append(
        store,
        "run",
        "tool_started",
        {
            "tool_call_id": "read",
            "tool_name": "read_file",
            "risky": False,
            "effect_scope": "none",
            "potential_effects": [],
        },
    )
    append(
        store,
        "run",
        "tool_result",
        {
            "outcome": read_outcome().to_dict(),
        },
    )

    summary = store.replay("run").summary()
    result = store.read_events("run")[-1]
    assert set(result.payload) == {"outcome"}
    assert result.call_id == "read"
    assert result.name == "read_file"
    assert summary["metrics"]["executed_tool_count"] == 1
    assert summary["metrics"]["tool_counts"] == {"read_file": 1}
    assert summary["pending_call_id"] is None
    assert run_main(["show", "run", "--cwd", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["run_cursor"]["sequence"] == 4


def test_load_run_returns_the_same_event_snapshot_used_by_replay(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    append(store, "run", "user_message", {"content": "inspect"})
    append(store, "run", "run_started", {"task_id": "task", "workspace_root": "/w"})

    events, loaded = store.load_run("run")
    replayed = store.replay("run")

    assert events == tuple(store.read_events("run"))
    assert loaded.summary() == replayed.summary()
    assert loaded.last_cursor.event_id == events[-1].event_id


def test_projection_tracks_exactly_one_pending_call(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    append(store, "run", "user_message", {"content": "inspect"})
    append(
        store,
        "run",
        "assistant_tool_call",
        {"name": "read_file", "args": {}, "call_id": "read"},
    )
    assert store.replay("run").pending_call_id == "read"
    with pytest.raises(ValueError, match="already has a pending"):
        append(
            store,
            "run",
            "assistant_tool_call",
            {"name": "search", "args": {}, "call_id": "other"},
        )


def test_task_contract_is_first_event_and_goal_cannot_be_overwritten(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    original = "repair the original task"
    append(store, "run", "user_message", {"content": original})
    append(store, "run", "run_resumed", {"task_id": "task", "workspace_root": "/w"})
    assert store.replay("run").task.contract.goal == original
    with pytest.raises(ValueError, match="only one user_message"):
        append(store, "run", "user_message", {"content": "replace"})


def test_run_log_repairs_only_incomplete_tail(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    append(store, "tail", "user_message", {"content": "inspect"})
    path = store.events_path("tail")
    with path.open("ab") as handle:
        handle.write(b'{"incomplete":')
    assert len(store.read_events("tail")) == 1
    assert path.read_bytes().endswith(b"\n")

    with path.open("ab") as handle:
        handle.write(b"{bad json}\n")
    with pytest.raises(ValueError, match="not valid JSON"):
        store.read_events("tail")


def test_v15_rejects_legacy_payload_shapes():
    with pytest.raises(ValueError, match="invalid user_message payload"):
        RunEvent("e", 1, "run", "task", "session", "user_message", "now", {"content": "old"})

    value = RunEvent(
        "e",
        1,
        "run",
        "task",
        "session",
        "user_message",
        "now",
        {"contract": TaskContract("inspect", **READ_TASK).to_dict()},
    ).to_dict()
    value["schema_version"] = "run-log-v14"
    with pytest.raises(ValueError, match="invalid Run event"):
        RunEvent.from_dict(value)

    with pytest.raises(ValueError, match="requires a call id"):
        RunEvent(
            "call",
            2,
            "run",
            "task",
            "session",
            "assistant_tool_call",
            "now",
            {"name": "read_file", "args": {}, "call_id": ""},
        )

    with pytest.raises(ValueError, match="invalid tool_result payload"):
        RunEvent(
            "result",
            2,
            "run",
            "task",
            "session",
            "tool_result",
            "now",
            {
                "tool_call_id": "read",
                "tool_name": "read_file",
                "outcome": read_outcome().to_dict(),
            },
        )

    with pytest.raises(ValueError, match="tool outcome requires a call id"):
        ToolOutcome(
            "",
            "read_file",
            "success",
            "completed",
            "none",
            "read",
        )

    malformed = TaskContract("inspect", **READ_TASK).to_dict()
    malformed["goal"] = 123
    with pytest.raises(TypeError, match="goal must be a string"):
        RunEvent(
            "e",
            1,
            "run",
            "task",
            "session",
            "user_message",
            "now",
            {"contract": malformed},
        )


def test_tool_result_rejects_workspace_revision_and_correction_fields(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    log = RunLog("run", "task", "session", store)
    log.append_user(TaskContract("inspect", **READ_TASK))
    log.append_model_instruction("historical fact that must be summarized")
    call = ToolCall("read_file", {"path": "README.md"}, "read")
    log.append_tool_call(call)
    log.append_tool_started(call, risky=False, effect_scope="none", potential_effects=[])
    payload = read_outcome().to_dict()
    assert "correction_action" not in payload
    with pytest.raises(TypeError):
        log.append_tool_result(read_outcome(), workspace_revision=0)


def test_mismatched_tool_result_is_not_persisted(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    log = RunLog("run", "task", "session", store)
    log.append_user(TaskContract("inspect", **READ_TASK))
    log.append_tool_call(ToolCall("read_file", {}, "expected"))
    wrong = ToolOutcome(
        "wrong",
        "read_file",
        "rejected",
        "not_started",
        "none",
        "no",
        failure=FailureInfo("rejected", "rejected", "no_retry"),
    )
    with pytest.raises(ValueError, match="pending tool call"):
        log.append_tool_result(wrong)
    assert [event.kind for event in log.events] == [
        "user_message",
        "assistant_tool_call",
    ]


def test_terminal_only_persists_final_diff_and_blocks_later_events(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    log = RunLog("run", "task", "session", store)
    log.append_user(TaskContract("inspect", **READ_TASK))
    terminal = log.append_final("done", FinalDiffDescriptor())
    assert terminal.payload["final_diff"] == FinalDiffDescriptor().to_dict()
    assert set(terminal.payload) == {
        "content",
        "stop_reason",
        "run_duration_ms",
        "final_diff",
    }
    with pytest.raises(ValueError, match="after a terminal event"):
        log.append_model_instruction("late")


def test_unavailable_final_diff_is_valid_only_for_stopped_runs(tmp_path):
    unavailable = FinalDiffDescriptor.unavailable("workspace_drift")
    assert unavailable.to_dict() == {
        "diff_artifact_id": "",
        "diff_bytes": 0,
        "unavailable_reason": "workspace_drift",
    }
    with pytest.raises(ValueError, match="cannot contain an artifact"):
        FinalDiffDescriptor(
            "diff_0000000000000000_0000000000",
            1,
            "workspace_drift",
        )

    store = RunStore(tmp_path / ".pico/runs")
    completed = RunLog("completed", "task", "session", store)
    completed.append_user(TaskContract("inspect", **READ_TASK))
    with pytest.raises(ValueError, match="requires an available final Diff"):
        completed.append_final("done", unavailable)
    assert [event.kind for event in completed.events] == ["user_message"]

    stopped = RunLog("stopped", "task", "session", store)
    stopped.append_user(TaskContract("inspect", **READ_TASK))
    stopped.append_stopped("stopped", "user_reset", unavailable)
    assert store.replay("stopped").final_diff == unavailable


def test_replay_rejects_diff_descriptor_without_net_changes(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    log = RunLog("run", "task", "session", store)
    log.append_user(TaskContract("inspect", **READ_TASK))
    log.append_final(
        "done",
        FinalDiffDescriptor("diff_0000000000000000_0000000000", 1),
    )
    with pytest.raises(ValueError, match="does not match net changes"):
        store.replay("run")


def test_run_store_rejects_escaping_run_ids(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    for run_id in ("", ".", "..", "../outside", "nested/run"):
        with pytest.raises(ValueError, match="invalid run id"):
            store.events_path(run_id)


def test_find_active_run_uses_last_event_time(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    append(store, "run_z", "user_message", {"content": "old"})
    append(store, "run_a", "user_message", {"content": "new"})
    run_id, events, projection = store.find_active_run("session")
    assert run_id == "run_a"
    assert events[0].content == "new"
    assert projection.task.contract.goal == "new"


def test_compaction_filters_canonical_state_but_covers_full_prefix(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    log = RunLog("run", "task", "session", store)
    log.append_user(TaskContract("inspect", **READ_TASK))
    log.append_model_instruction("historical fact that must be summarized")
    call = ToolCall(
        "update_working_state",
        {"add_next_steps": ["read"]},
        "state",
    )
    log.append_tool_call(call)
    log.append_tool_started(call, risky=False, effect_scope="none", potential_effects=[])
    log.append_tool_result(
        ToolOutcome(
            "state",
            "update_working_state",
            "success",
            "completed",
            "none",
            "accepted",
        )
    )
    log.append_model_instruction("recent")
    seen = []

    def summarize(events):
        seen.extend(events)
        return "short"

    result = log.compact(
        retain_tokens=1,
        history_token_counter=lambda text: max(1, len(text)),
        summary_builder=summarize,
    )
    assert result is not None
    compacted, _metadata = result
    assert [event.kind for event in seen] == ["model_instruction"]
    assert seen[0].content == "historical fact that must be summarized"
    assert len(compacted.covered_event_ids) == 4


def test_compaction_retain_budget_counts_one_complete_history_projection(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    log = RunLog("run", "task", "session", store)
    log.append_user(TaskContract("inspect", **READ_TASK))
    log.append_model_instruction("historical " * 30)
    recent_one = log.append_model_instruction("recent one")
    recent_two = log.append_model_instruction("recent two")

    def wire_tokens(text):
        return 100 + len(text)

    recent_projection = "\n".join(
        (
            "Current run events:",
            log._render_event(recent_one),
            log._render_event(recent_two),
        )
    )
    result = log.compact(
        retain_tokens=wire_tokens(recent_projection),
        history_token_counter=wire_tokens,
        summary_builder=lambda _events: "short",
    )

    assert result is not None
    _event, metadata = result
    assert metadata["retained_events"] == 2
    assert metadata["retained_tokens"] == wire_tokens(recent_projection)


def test_consecutive_compactions_replace_the_active_logical_prefix(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    log = RunLog("run", "task", "session", store)
    user = log.append_user(TaskContract("inspect", **READ_TASK))
    old = log.append_model_instruction("old")
    recent = log.append_model_instruction("recent")

    first = log._commit_compaction(
        "first summary",
        [user.event_id, old.event_id],
    )
    later = log.append_model_instruction("later")
    assert [event.event_id for event in log.active_events()] == [
        first.event_id,
        recent.event_id,
        later.event_id,
    ]

    second = log._commit_compaction(
        "second summary",
        [first.event_id, recent.event_id],
    )
    expected = [second.event_id, later.event_id]

    assert [event.event_id for event in log.active_events()] == expected
    restored = RunLog.restore("run", store)
    assert [event.event_id for event in restored.active_events()] == expected


def test_compacted_history_keeps_summary_and_only_complete_recent_units(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    log = RunLog("run", "task", "session", store)
    user = log.append_user(TaskContract("inspect", **READ_TASK))
    old = log.append_model_instruction("old")
    calls = []
    for index in range(2):
        call = ToolCall("read_file", {"path": f"f{index}.py"}, f"call_{index}")
        calls.append(call)
        log.append_tool_call(call)
        log.append_tool_started(
            call,
            risky=False,
            effect_scope="none",
            potential_effects=[],
        )
        log.append_tool_result(
            ToolOutcome(
                call.call_id,
                call.name,
                "success",
                "completed",
                "none",
                "result " + "x" * 250,
            )
        )
    log._commit_compaction("SUMMARY-MARKER", [user.event_id, old.event_id])

    rendered, metadata = log.render_compacted_projection(
        retain_tokens=600,
        token_counter=len,
    )

    assert "SUMMARY-MARKER" in rendered
    assert rendered.count("[assistant/tool]") == rendered.count("[tool/read_file/")
    assert rendered.count("[assistant/tool]") == 1
    assert "f1.py" in rendered
    assert "f0.py" not in rendered
    assert "recent events omitted by History budget" in rendered
    assert metadata["projection_mode"] == "compacted_complete_transactions"
