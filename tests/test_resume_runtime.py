import pytest

from pico import (
    FakeModelClient,
    ModelAction,
    Pico,
    PicoConfig,
    SessionStore,
    ToolCall,
    WorkspaceContext,
)
from pico.run_log import RunLog
from pico.run_projection import RunProjection
from pico.runtime_recovery import RESUME_READY
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


def build_interrupted_run(tmp_path, config=None):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    store = SessionStore(tmp_path / ".pico/sessions")
    agent = Pico(
        FakeModelClient([]),
        WorkspaceContext.build(tmp_path),
        store,
        config=config or PicoConfig(approval_policy="auto", verification_command=""),
    )
    contract = TaskContract("Inspect", **NO_CHANGE_TASK)
    log = RunLog(
        "run_interrupted",
        "task_interrupted",
        agent.session.data["id"],
        agent.dependencies.run_store,
    )
    first = log.append_user(contract)
    projection = RunProjection().apply_event(first)
    agent.run.projection = projection
    agent.run.run_log = log
    agent.session.set_active_run(projection.run_id)
    return agent, store, projection, log


def resumed_agent(agent, store, outputs, config=None):
    return Pico(
        FakeModelClient(outputs),
        WorkspaceContext.build(agent.workspace.root),
        store,
        session=store.load(agent.session.data["id"]),
        run_store=agent.dependencies.run_store,
        config=config or PicoConfig(approval_policy="auto", verification_command=""),
    )


def test_active_run_restores_same_projection(tmp_path):
    agent, store, projection, log = build_interrupted_run(tmp_path)
    call = ToolCall("read_file", {"path": "README.md"}, "read")
    agent.apply_run_event(log.append_tool_call(call))
    assert agent.tools.execute(call).status == "success"
    agent.session.set_active_run("")

    resumed = resumed_agent(agent, store, [ModelAction.final("Recovered.")])
    assert resumed.recovery.state["status"] == RESUME_READY
    assert resumed.ask("Continue", **NO_CHANGE_TASK) == "Recovered."
    assert resumed.run.projection.run_id == projection.run_id
    assert resumed.run.task.contract.goal == "Inspect"
    assert resumed.run.projection.pending_call_id is None


def test_incremental_working_state_restores_from_tool_events(tmp_path):
    agent, store, projection, log = build_interrupted_run(tmp_path)
    call = ToolCall(
        "update_working_state",
        {
            "add_constraints": ["Keep schema"],
            "add_decisions": ["Fix refresh"],
            "add_next_steps": ["Add test"],
        },
        "state",
    )
    agent.apply_run_event(log.append_tool_call(call))
    assert agent.tools.execute(call).status == "success"

    resumed = resumed_agent(agent, store, [ModelAction.final("Recovered.")])
    assert resumed.ask("Continue", **NO_CHANGE_TASK) == "Recovered."
    assert resumed.run.task.contract.goal == projection.task.contract.goal
    assert resumed.run.task.working.constraints == ("Keep schema",)
    assert resumed.run.task.working.decisions == ("Fix refresh",)
    assert resumed.run.task.working.next_steps == ("Add test",)


def test_resume_rejects_requirement_change_without_consuming_recovery(tmp_path):
    agent, store, _projection, _log = build_interrupted_run(tmp_path)
    resumed = resumed_agent(agent, store, [ModelAction.final("Recovered.")])
    with pytest.raises(ValueError, match="do not match"):
        resumed.ask("Continue", **READ_TASK)
    assert resumed.recovery.state["status"] == RESUME_READY
    assert resumed.ask("Continue", **NO_CHANGE_TASK) == "Recovered."


def test_user_contract_is_durable_before_session_pointer(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / ".pico/sessions")
    agent = Pico(
        FakeModelClient([]),
        WorkspaceContext.build(tmp_path),
        store,
        config=PicoConfig(approval_policy="auto", verification_command=""),
    )
    original = agent.session.set_active_run
    captured = ""

    def fail_pointer(run_id):
        nonlocal captured
        captured = str(run_id)
        events = agent.dependencies.run_store.read_events(captured)
        assert [event.kind for event in events] == ["user_message"]
        assert events[0].payload["contract"]["goal"] == "Persist"
        raise OSError("pointer failed")

    monkeypatch.setattr(agent.session, "set_active_run", fail_pointer)
    with pytest.raises(OSError, match="pointer failed"):
        agent.ask("Persist", **NO_CHANGE_TASK)
    monkeypatch.setattr(agent.session, "set_active_run", original)
    agent.model_client.outputs.append(ModelAction.final("Recovered."))
    assert agent.ask("Continue", **NO_CHANGE_TASK) == "Recovered."
    assert agent.run.projection.run_id == captured


def test_terminal_run_starts_a_new_run(tmp_path):
    agent = Pico(
        FakeModelClient([ModelAction.final("First.")]),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        config=PicoConfig(approval_policy="auto", verification_command=""),
    )
    assert agent.ask("First", **NO_CHANGE_TASK) == "First."
    first_run = agent.run.projection.run_id
    resumed = resumed_agent(agent, agent.session.store, [ModelAction.final("Second.")])
    assert resumed.ask("Second", **NO_CHANGE_TASK) == "Second."
    assert resumed.run.projection.run_id != first_run


def test_persisted_call_without_start_is_not_replayed(tmp_path):
    agent, store, _projection, log = build_interrupted_run(tmp_path)
    call = ToolCall("write_file", {"path": "x.txt", "content": "x\n"}, "write")
    log.append_tool_call(call)
    resumed = resumed_agent(agent, store, [ModelAction.final("Recovered.")])
    assert resumed.ask("Continue", **NO_CHANGE_TASK) == "Recovered."
    result = next(
        event
        for event in resumed.run.run_log.events
        if event.kind == "tool_result" and event.call_id == "write"
    )
    assert result.payload["outcome"]["execution_state"] == "not_started"
    assert not (tmp_path / "x.txt").exists()


def test_started_unchanged_path_recovers_as_no_effect_error(tmp_path):
    agent, store, _projection, log = build_interrupted_run(tmp_path)
    call = ToolCall("write_file", {"path": "x.txt", "content": "x\n"}, "write")
    log.append_tool_call(call)
    log.append_tool_started(
        call,
        risky=True,
        effect_scope="workspace",
        potential_effects=[
            {"path": "x.txt", "before_state": "absent", "before_artifact_id": ""}
        ],
    )
    resumed = resumed_agent(agent, store, [ModelAction.final("Recovered.")])
    assert resumed.ask("Continue", **NO_CHANGE_TASK) == "Recovered."
    result = next(event for event in resumed.run.run_log.events if event.call_id == "write" and event.kind == "tool_result")
    assert result.outcome_status == "error"
    assert result.side_effect_state == "none"


def test_started_changed_path_recovers_as_partial_without_replay(tmp_path):
    agent, store, _projection, log = build_interrupted_run(tmp_path)
    call = ToolCall("write_file", {"path": "x.txt", "content": "x\n"}, "write")
    log.append_tool_call(call)
    log.append_tool_started(
        call,
        risky=True,
        effect_scope="workspace",
        potential_effects=[
            {"path": "x.txt", "before_state": "absent", "before_artifact_id": ""}
        ],
    )
    (tmp_path / "x.txt").write_text("side effect\n", encoding="utf-8")
    resumed = resumed_agent(agent, store, [])
    restored = RunLog.restore("run_interrupted", resumed.dependencies.run_store)
    resumed.run.projection = resumed.dependencies.run_store.replay("run_interrupted")
    resumed.run.run_log = restored
    restored.reconcile_interrupted(resumed)
    result = restored.events[-1]
    assert result.outcome_status == "partial_success"
    assert result.side_effect_state == "partial"
    assert result.affected_paths == ("x.txt",)
