"""Check compaction evaluation metrics using current Run Log events."""

from pico import TaskContract, ToolCall, ToolOutcome
from pico.run_log import RunLog
from pico.run_store import RunStore
from scripts.run_real_compaction import analyze_run


def test_compaction_preservation_is_measured_before_completed_steps_are_cleared(tmp_path):
    log = RunLog("run", "task", "session", RunStore(tmp_path / "runs"))
    first = log.append_user(TaskContract("inspect", False, False))

    def update(args, call_id):
        call = ToolCall("update_working_state", args, call_id)
        log.append_tool_calls((call,))
        log.append_tool_started(call, effect_scope="none", potential_effects=[])
        log.append_tool_result(ToolOutcome(call_id, call.name, "success", "completed", "none", "accepted"))

    update({"add_constraints": ["Preserve API"], "add_next_steps": ["Edit"]}, "plan")
    log.append_compaction("summary", [first.event_id])
    update({"remove_next_steps": ["Edit"]}, "finish")
    analysis = analyze_run(log.events)
    assert analysis["working_state"]["next_steps"] == []
    assert analysis["working_state_preserved"]
    row, = analysis["compactions"]
    assert row["working_state_before"] == row["working_state_after"]
    assert row["working_state_before"]["next_steps"] == ["Edit"]


def test_compaction_does_not_claim_to_preserve_a_never_populated_whiteboard(tmp_path):
    log = RunLog("run", "task", "session", RunStore(tmp_path / "runs"))
    first = log.append_user(TaskContract("inspect", False, False))
    log.append_compaction("summary", [first.event_id])
    assert not analyze_run(log.events)["working_state_preserved"]
