
import json

import pytest

from pico.contracts import FailureInfo, ToolCall, ToolOutcome
from pico.delivery import FinalDiff
from pico.history import RunHistory
from pico.run_cli import run_main
from pico.run_log import RunEvent, RunLog, replay_events
from pico.run_projection import RunOutcome, RunProjection
from pico.run_store import RunStore
from pico.task_state import TaskContract
from pico.tools import build_tool_registry

READ_TASK = {
    "allows_workspace_mutation": False,
    "verify_changes": False,
}
NO_CHANGE_TASK = {
    "allows_workspace_mutation": True,
    "verify_changes": False,
}
MODIFY_TASK = {
    "allows_workspace_mutation": True,
    "verify_changes": False,
}


def task_contract(goal="inspect"):
    return TaskContract(
        goal,
        allows_workspace_mutation=True,
        verify_changes=False,
    )


def append(store, run_id, kind, payload=None):
    payload = dict(payload or {})
    if kind == "user_message":
        payload = {
            "contract": task_contract(payload.pop("content")).to_dict()
        }
    log = (store.load_run(run_id)[0] if store.has_events(run_id)
           else RunLog(run_id, "task", "session", store))
    return log.append(kind, payload)


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
        "assistant_tool_calls",
        {"calls": [{"name": "read_file", "args": {"path": "README.md"}, "call_id": "read"}]},
    )
    append(
        store,
        "run",
        "tool_started",
        {
            "tool_call_id": "read",
            "tool_name": "read_file",
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
    assert summary["pending_call_ids"] == []
    assert run_main(["show", "run", "--cwd", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["run_cursor"]["sequence"] == 4


def test_replay_snapshots_one_iterable_and_validates_event_identity(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    append(store, "run", "user_message", {"content": "inspect"})
    events = tuple(store.read_events("run"))

    replayed = replay_events(iter(events))
    assert replayed.run_id == "run"
    assert replayed.contract.goal == "inspect"

    malformed = RunEvent(
        event_id="arbitrary",
        sequence=7,
        run_id="run",
        task_id="task",
        session_id="session",
        kind="user_message",
        timestamp="now",
        payload={"contract": task_contract().to_dict()},
    )
    with pytest.raises(ValueError, match="sequence is not contiguous"):
        replay_events([malformed])


def test_load_run_returns_the_same_event_snapshot_used_by_replay(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    append(store, "run", "user_message", {"content": "inspect"})
    append(store, "run", "run_started", {"task_id": "task", "workspace_root": "/w"})

    log, loaded = store.load_run("run")
    events = log.events
    replayed = store.replay("run")

    assert events == tuple(store.read_events("run"))
    assert loaded.summary() == replayed.summary()
    assert loaded.last_cursor.event_id == events[-1].event_id


def test_load_run_reads_once_and_checks_protocol_once_per_event(tmp_path, monkeypatch):
    store = RunStore(tmp_path / "runs")
    log = RunLog("run", "task", "session", store)
    log.append_user(task_contract())
    call = ToolCall("read_file", {"path": "README.md"}, "read")
    log.append_tool_calls((call,))
    reads = []
    checks = []
    original_read = store._read_events
    original_check = RunProjection.check_event

    def read(run_id):
        reads.append(run_id)
        return original_read(run_id)

    def check(projection, event):
        checks.append(event.kind)
        return original_check(projection, event)

    monkeypatch.setattr(store, "_read_events", read)
    monkeypatch.setattr(RunProjection, "check_event", check)
    restored, projection = store.load_run("run")
    assert reads == ["run"]
    assert checks == ["user_message", "assistant_tool_calls"]
    assert restored.pending_tool_calls() == (call,)
    assert projection.pending_call_id == call.call_id

    started = restored.append_tool_started(call, effect_scope="none", potential_effects=[])
    assert started.sequence == 3
    assert started.event_id == "run:event:000003"
    assert checks == ["user_message", "assistant_tool_calls", "tool_started"]


def test_log_passes_complete_event_to_storage_before_advancing_state(tmp_path, monkeypatch):
    store = RunStore(tmp_path / "runs")
    log = RunLog("run", "task", "session", store)
    persist = store._append_event
    accepted = []

    def save(event):
        assert isinstance(event, RunEvent)
        assert log.events == ()
        accepted.append(event)
        persist(event)

    monkeypatch.setattr(store, "_append_event", save)
    first = log.append_user(task_contract())
    assert first is accepted[0]
    assert log.events == (first,)
    with pytest.raises(ValueError, match="only one user_message"):
        log.append_user(task_contract())
    assert len(accepted) == 1


def test_loaded_log_rejects_another_run_identity_before_installing_state(tmp_path):
    store = RunStore(tmp_path / "runs")
    log = RunLog("run", "task", "session", store)
    log.append_user(task_contract())
    other = store.events_path("other")
    other.parent.mkdir()
    other.write_bytes(store.events_path("run").read_bytes())
    with pytest.raises(ValueError, match="belongs to another run"):
        store.load_run("other")
    with pytest.raises(ValueError, match="belongs to another run"):
        store.read_events("other")


def test_projection_tracks_one_pending_tool_transaction(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    append(store, "run", "user_message", {"content": "inspect"})
    append(
        store,
        "run",
        "assistant_tool_calls",
        {"calls": [{"name": "read_file", "args": {}, "call_id": "read"}]},
    )
    assert store.replay("run").pending_call_id == "read"
    with pytest.raises(ValueError, match="already has pending"):
        append(
            store,
            "run",
            "assistant_tool_calls",
            {"calls": [{"name": "search", "args": {}, "call_id": "other"}]},
        )


def test_tool_group_round_trips_in_original_order(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    log = RunLog("run", "task", "session", store)
    log.append_user(task_contract())
    calls = (
        ToolCall("read_file", {"path": "a.py"}, "call_a"),
        ToolCall("search", {"pattern": "needle", "path": "."}, "call_b"),
    )
    group = log.append_tool_calls(calls)

    assert group.event_id == "run:event:000002"
    assert tuple(call.call_id for call in group.tool_calls) == (
        "call_a",
        "call_b",
    )
    assert store.replay("run").pending_call_ids == ("call_a", "call_b")
    assert store.replay("run").pending_call_id is None

    for call in calls:
        log.append_tool_started(call, effect_scope="none", potential_effects=[])
    for call in calls:
        log.append_tool_result(
            ToolOutcome(
                call.call_id,
                call.name,
                "success",
                "completed",
                "none",
                f"result for {call.call_id}",
            )
        )

    replayed = store.replay("run")
    assert replayed.pending_call_ids == ()
    assert [event.call_id for event in RunHistory(log.events).context_events() if event.kind == "tool_result"] == [
        "call_a",
        "call_b",
    ]


def test_grouped_working_state_updates_project_in_result_order(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    log = RunLog("run", "task", "session", store)
    log.append_user(task_contract())
    calls = (
        ToolCall(
            "update_working_state",
            {"add_next_steps": ["read evidence"]},
            "state_add",
        ),
        ToolCall(
            "update_working_state",
            {
                "remove_next_steps": ["read evidence"],
                "add_decisions": ["evidence reviewed"],
            },
            "state_finish",
        ),
        ToolCall("read_file", {"path": "a.py"}, "read"),
    )
    log.append_tool_calls(calls)
    for call in calls:
        log.append_tool_started(call, effect_scope="none", potential_effects=[])
        log.append_tool_result(
            ToolOutcome(
                call.call_id,
                call.name,
                "success",
                "completed",
                "none",
                "accepted" if call.name == "update_working_state" else "observed",
            )
        )

    restored = store.replay("run")
    history, _metadata = RunHistory(log.events).render_projection()

    assert log.projection.working.next_steps == ()
    assert log.projection.working.decisions == ("evidence reviewed",)
    assert restored.working.to_dict() == log.projection.working.to_dict()
    assert "update_working_state" not in history
    assert "[assistant/tool] read_file" in history


@pytest.mark.parametrize(
    "payload",
    [
        {"calls": []},
        {
            "calls": [
                {"name": "read_file", "args": {}, "call_id": "same"},
                {"name": "search", "args": {}, "call_id": "same"},
            ],
        },
        {
            "calls": [
                {"name": "read_file", "args": {}, "call_id": "a"},
                {"name": "search", "args": {}, "call_id": ""},
            ],
        },
    ],
)
def test_tool_group_payload_is_strict(payload):
    with pytest.raises(ValueError):
        RunEvent(
            "event",
            2,
            "run",
            "task",
            "session",
            "assistant_tool_calls",
            "now",
            payload,
        )


def test_tool_group_rejects_out_of_order_results(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    log = RunLog("run", "task", "session", store)
    log.append_user(task_contract())
    calls = (
        ToolCall("read_file", {"path": "a.py"}, "call_a"),
        ToolCall("read_file", {"path": "b.py"}, "call_b"),
    )
    log.append_tool_calls(calls)
    for call in calls:
        log.append_tool_started(call, effect_scope="none", potential_effects=[])

    with pytest.raises(ValueError, match="preserve group order"):
        log.append_tool_result(
            ToolOutcome(
                "call_b",
                "read_file",
                "success",
                "completed",
                "none",
                "b",
            )
        )
    with pytest.raises(RuntimeError, match="pending tool calls"):
        log.append_model_instruction("test_instruction", "cannot interleave")


def test_tool_group_rejects_start_across_unfinished_execution_barrier(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    log = RunLog("run", "task", "session", store)
    log.append_user(task_contract())
    calls = tuple(
        ToolCall("read_file", {"path": f"{name}.py"}, f"call_{name}")
        for name in ("a", "b", "c")
    )
    log.append_tool_calls(calls)
    log.append_tool_started(calls[0], effect_scope="none", potential_effects=[])
    log.append_tool_started(calls[1], effect_scope="none", potential_effects=[])
    log.append_tool_result(
        ToolOutcome("call_a", "read_file", "success", "completed", "none", "a")
    )

    with pytest.raises(ValueError, match="unfinished execution barrier"):
        log.append_tool_started(calls[2], effect_scope="none", potential_effects=[])

    assert log.pending_tool_calls() == calls[1:]


def test_task_contract_is_first_event_and_goal_cannot_be_overwritten(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    original = "repair the original task"
    append(store, "run", "user_message", {"content": original})
    append(store, "run", "run_resumed", {"task_id": "task", "workspace_root": "/w"})
    assert store.replay("run").contract.goal == original
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


def test_rejects_legacy_payload_shapes():
    with pytest.raises(ValueError, match="unsupported Run Log kind"):
        RunEvent(
            "legacy",
            2,
            "run",
            "task",
            "session",
            "assistant_tool_batch",
            "now",
            {"batch_id": "batch", "calls": []},
        )

    with pytest.raises(ValueError, match="invalid user_message payload"):
        RunEvent("e", 1, "run", "task", "session", "user_message", "now", {"content": "old"})

    with pytest.raises(ValueError, match="invalid model_instruction payload"):
        RunEvent(
            "instruction",
            2,
            "run",
            "task",
            "session",
            "model_instruction",
            "now",
            {"content": "legacy"},
        )

    with pytest.raises(ValueError, match="unsupported Run Log kind"):
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

    malformed = task_contract().to_dict()
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


def test_mismatched_tool_result_is_not_persisted(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    log = RunLog("run", "task", "session", store)
    log.append_user(task_contract())
    log.append_tool_calls((ToolCall("read_file", {}, "expected"),))
    wrong = ToolOutcome(
        "wrong",
        "read_file",
        "rejected",
        "not_started",
        "none",
        "no",
        failure=FailureInfo("rejected", "rejected", "no_retry"),
    )
    with pytest.raises(ValueError, match="preserve group order"):
        log.append_tool_result(wrong)
    assert [event.kind for event in log.events] == [
        "user_message",
        "assistant_tool_calls",
    ]


def test_terminal_only_persists_final_diff_and_blocks_later_events(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    log = RunLog("run", "task", "session", store)
    log.append_user(task_contract())
    terminal = log.append_final("done", FinalDiff())
    assert terminal.payload["final_diff"] == FinalDiff().to_dict()
    assert set(terminal.payload) == {
        "content",
        "stop_reason",
        "turn_duration_ms",
        "final_diff",
    }
    with pytest.raises(ValueError, match="after a terminal event"):
        log.append_model_instruction("test_instruction", "late")


def test_stopped_run_may_omit_an_unavailable_final_diff(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    stopped = RunLog("stopped", "task", "session", store)
    stopped.append_user(task_contract())
    event = stopped.append_stopped("stopped", "user_reset")
    assert "final_diff" not in event.payload
    projection = store.replay("stopped")
    assert projection.final_diff is None
    assert RunOutcome(projection).to_dict()["final_diff"] is None

    completed = RunLog("completed", "task", "session", store)
    completed.append_user(task_contract())
    with pytest.raises(TypeError, match="requires a FinalDiff"):
        completed.append_final("done", None)


def test_replay_rejects_diff_descriptor_without_net_changes(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    log = RunLog("run", "task", "session", store)
    log.append_user(task_contract())
    with pytest.raises(ValueError, match="does not match net changes"):
        log.append_final("done", FinalDiff("diff_0000000000000000_0000000000", 1))


def test_run_store_rejects_escaping_run_ids(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    for run_id in ("", ".", "..", "../outside", "nested/run"):
        with pytest.raises(ValueError, match="invalid run id"):
            store.events_path(run_id)


def test_find_active_run_uses_last_event_time(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    append(store, "run_z", "user_message", {"content": "old"})
    append(store, "run_a", "user_message", {"content": "new"})
    log, projection = store.find_active_run("session")
    assert log.run_id == "run_a"
    assert log.events[0].content == "new"
    assert projection.contract.goal == "new"


def test_compaction_filters_canonical_state_but_covers_full_prefix(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    log = RunLog("run", "task", "session", store)
    log.append_user(task_contract())
    log.append_model_instruction("test_instruction", "historical fact that must be summarized")
    call = ToolCall(
        "update_working_state",
        {"add_next_steps": ["read"]},
        "state",
    )
    log.append_tool_calls((call,))
    log.append_tool_started(call, effect_scope="none", potential_effects=[])
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
    log.append_model_instruction("test_instruction", "recent")
    seen = []

    def summarize(events):
        seen.extend(events)
        return "short"

    result = RunHistory(log.events).plan_compaction(
        retain_tokens=1,
        history_token_counter=lambda text: max(1, len(text)),
        summary_builder=summarize,
    )
    assert result is not None
    summary, covered, _metadata = result
    compacted = log.append_compaction(summary, covered)
    assert [event.kind for event in seen] == ["model_instruction"]
    assert seen[0].payload["instruction"] == "historical fact that must be summarized"
    assert len(compacted.covered_event_ids) == 5


def test_compaction_retain_budget_counts_one_complete_history_projection(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    log = RunLog("run", "task", "session", store)
    log.append_user(task_contract())
    log.append_model_instruction("test_instruction", "historical " * 30)
    recent_one = log.append_model_instruction("test_instruction", "recent one")
    recent_two = log.append_model_instruction("test_instruction", "recent two")

    def wire_tokens(text):
        return 100 + len(text)

    recent_projection = "\n".join(
        (
            "Current run events:",
            RunHistory._render_fact(RunHistory._event_fact(recent_one)),
            RunHistory._render_fact(RunHistory._event_fact(recent_two)),
        )
    )
    result = RunHistory(log.events).plan_compaction(
        retain_tokens=wire_tokens(recent_projection),
        history_token_counter=wire_tokens,
        summary_builder=lambda _events: "short",
    )

    assert result is not None
    _summary, _covered, metadata = result
    assert metadata["retained_events"] == 2
    assert metadata["retained_tokens"] == wire_tokens(
        "\n".join(
            (
                "Current run events:",
                RunHistory._render_fact(RunHistory._event_fact(recent_one)),
            )
        )
    )


def test_consecutive_compactions_replace_the_active_logical_prefix(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    log = RunLog("run", "task", "session", store)
    user = log.append_user(task_contract())
    old = log.append_model_instruction("test_instruction", "old")
    recent = log.append_model_instruction("test_instruction", "recent")

    first = log.append_compaction(
        "first summary",
        [user.event_id, old.event_id],
    )
    later = log.append_model_instruction("test_instruction", "later")
    assert [event.event_id for event in RunHistory(log.events).active_events()] == [
        first.event_id,
        recent.event_id,
        later.event_id,
    ]

    second = log.append_compaction(
        "second summary",
        [first.event_id, recent.event_id],
    )
    expected = [second.event_id, later.event_id]

    assert [event.event_id for event in RunHistory(log.events).active_events()] == expected
    restored, _projection = store.load_run("run")
    assert [event.event_id for event in RunHistory(restored.events).active_events()] == expected


def test_compacted_history_keeps_summary_and_only_complete_recent_units(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    log = RunLog("run", "task", "session", store)
    user = log.append_user(task_contract())
    old = log.append_model_instruction("test_instruction", "old")
    calls = []
    for index in range(2):
        call = ToolCall("read_file", {"path": f"f{index}.py"}, f"call_{index}")
        calls.append(call)
        log.append_tool_calls((call,))
        log.append_tool_started(
            call,
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
    log.append_compaction("SUMMARY-MARKER", [user.event_id, old.event_id])

    rendered, metadata = RunHistory(log.events).render_compacted_projection(
        retain_tokens=600,
        token_counter=len,
    )

    assert "SUMMARY-MARKER" in rendered
    assert rendered.count("[assistant/tool]") == rendered.count("[tool/read_file/")
    assert rendered.count("[assistant/tool]") == 1
    assert "f1.py" in rendered
    assert "f0.py" not in rendered
    assert "recent events omitted by History budget" in rendered
    assert metadata["projection_mode"] == "compacted_call_transactions"


def test_tool_group_history_is_bounded_per_call(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    log = RunLog("run", "task", "session", store)
    user = log.append_user(task_contract())
    old = log.append_model_instruction("test_instruction", "old")
    calls = (
        ToolCall("read_file", {"path": "a.py"}, "call_a"),
        ToolCall("read_file", {"path": "b.py"}, "call_b"),
    )
    group = log.append_tool_calls(calls)
    for call in calls:
        log.append_tool_started(call, effect_scope="none", potential_effects=[])
    for call in calls:
        log.append_tool_result(
            ToolOutcome(
                call.call_id,
                call.name,
                "success",
                "completed",
                "none",
                "result " + call.call_id,
            )
        )

    projectors = {
        name: tool["history_projection"]
        for name, tool in build_tool_registry().items()
    }
    rendered, _metadata = RunHistory(
        log.events,
        history_projectors=projectors,
    ).render_recent_projection(
        retain_tokens=320,
        token_counter=len,
    )

    assert len(rendered) <= 320
    assert "call_b" in rendered
    assert "call_a" not in rendered
    with pytest.raises(ValueError, match="cannot split"):
        log.append_compaction(
            "invalid boundary",
            [user.event_id, old.event_id, group.event_id],
        )


def test_compact_tool_history_preserves_tool_owned_recovery_fields(tmp_path):
    store = RunStore(tmp_path / ".pico/runs")
    log = RunLog("run", "task", "session", store)
    log.append_user(task_contract())
    calls = (
        ToolCall(
            "search",
            {"pattern": "SECOND_NEEDLE", "path": "tests"},
            "search_call",
        ),
        ToolCall(
            "read_file",
            {"path": "subject.py", "start_line": 40, "end_line": 80},
            "read_call",
        ),
    )
    log.append_tool_calls(calls)
    outcomes = (
        ToolOutcome(
            "search_call",
            "search",
            "success",
            "completed",
            "none",
            "x" * 2000,
            structured={
                "engine": "rg",
                "match_count": 20,
                "truncated": True,
                "timed_out": False,
            },
        ),
        ToolOutcome(
            "read_call",
            "read_file",
            "success",
            "completed",
            "none",
            "y" * 2000,
            structured={
                "path": "subject.py",
                "start_line": 40,
                "end_line": 80,
                "total_lines": 200,
                "has_more": True,
                "truncated": False,
                "revision": "sha256:" + "a" * 64,
            },
        ),
    )
    for call, outcome in zip(calls, outcomes):
        log.append_tool_started(call, effect_scope="none", potential_effects=[])
        log.append_tool_result(outcome)
    projectors = {
        name: tool["history_projection"]
        for name, tool in build_tool_registry().items()
    }

    rendered, metadata = RunHistory(
        log.events,
        history_projectors=projectors,
    ).render_recent_projection(retain_tokens=1000, token_counter=len)

    assert metadata["retained_tokens"] <= 1000
    assert "[tool receipt]" in rendered
    assert "SECOND_NEEDLE" in rendered
    assert '"start_line":40' in rendered
    assert '"has_more":true' in rendered
    assert "sha256:" + "a" * 64 in rendered
