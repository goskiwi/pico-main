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
from pico.runtime_recovery import RESUME_NONE, RESUME_READY
from pico.task_state import TaskState


def build_interrupted_run(tmp_path, *, config=None):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    store = SessionStore(tmp_path / ".pico/sessions")
    agent = Pico(
        FakeModelClient([]),
        WorkspaceContext.build(tmp_path),
        store,
        config=config or PicoConfig(approval_policy="auto", verification_command=""),
    )
    state = TaskState.create("task_interrupted", "Inspect", run_id="run_interrupted")
    agent.run.task_state = state
    run_log = RunLog(
        state.run_id,
        state.task_id,
        agent.session.data["id"],
        agent.dependencies.run_store,
    )
    run_log.append_user("Inspect")
    agent.run.run_log = run_log
    agent.session.set_active_run(state.run_id)
    return agent, store, state, run_log


def resume_agent(agent, store, outputs, *, config=None):
    return Pico(
        FakeModelClient(outputs),
        WorkspaceContext.build(agent.workspace.root),
        store,
        session=store.load(agent.session.data["id"]),
        run_store=agent.dependencies.run_store,
        config=config or PicoConfig(approval_policy="auto", verification_command=""),
    )


def test_active_run_log_restores_same_run(tmp_path):
    agent, store, state, run_log = build_interrupted_run(tmp_path)
    call = ToolCall("read_file", {"path": "README.md"}, "call_read")
    run_log.append_tool_call(call)
    agent.tools.run(call)
    agent.session.set_active_run("")

    run_store = agent.dependencies.run_store
    original_read_events = run_store.read_events
    read_count = 0

    def counted_read_events(run_id):
        nonlocal read_count
        read_count += 1
        return original_read_events(run_id)

    run_store.read_events = counted_read_events

    resumed = resume_agent(agent, store, [ModelAction.final("Recovered.")])

    assert resumed.recovery.state["status"] == RESUME_READY
    assert resumed.ask("Continue") == "Recovered."
    assert resumed.run.task_state.run_id == state.run_id
    assert resumed.run.task_state.working_state.goal == "Inspect"
    assert "Inspect" in resumed.run.task_state.working_state.render_panel()
    assert "Continue" not in resumed.run.task_state.working_state.render_panel()
    assert read_count == 2
    assert resumed.recovery.state == {
        "status": RESUME_NONE,
        "active_run_id": "",
        "projection": None,
        "events": (),
    }


def test_structured_working_state_is_restored_from_tool_events(tmp_path):
    agent, store, state, run_log = build_interrupted_run(tmp_path)
    call = ToolCall(
        "update_working_state",
        {
            "add_constraints": ["Do not change the database schema"],
            "add_decisions": ["The failure is in token refresh"],
            "add_next_steps": ["Add a concurrent refresh test"],
        },
        "call_working_state",
    )
    agent.apply_run_event(run_log.append_tool_call(call))
    assert agent.tools.run(call).status == "success"

    resumed = resume_agent(agent, store, [ModelAction.final("Recovered.")])

    assert resumed.ask("Continue") == "Recovered."
    working = resumed.run.task_state.working_state
    assert working.goal == state.working_state.goal
    assert working.constraints == ("Do not change the database schema",)
    assert working.decisions == ("The failure is in token refresh",)
    assert working.next_steps == ("Add a concurrent refresh test",)


def test_new_run_writes_user_event_before_session_pointer(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / ".pico/sessions")
    agent = Pico(
        FakeModelClient([]),
        WorkspaceContext.build(tmp_path),
        store,
        config=PicoConfig(approval_policy="auto", verification_command=""),
    )
    original_set_active_run = agent.session.set_active_run
    captured_run_id = ""

    def fail_active_pointer(run_id):
        nonlocal captured_run_id
        captured_run_id = str(run_id)
        events = agent.dependencies.run_store.read_events(captured_run_id)
        assert [event.kind for event in events] == ["user_message"]
        raise OSError("session pointer failed")

    monkeypatch.setattr(agent.session, "set_active_run", fail_active_pointer)

    with pytest.raises(OSError, match="session pointer failed"):
        agent.ask("Persist this request")

    monkeypatch.setattr(agent.session, "set_active_run", original_set_active_run)
    agent.model_client.outputs.append(ModelAction.final("Recovered."))

    assert agent.ask("Continue") == "Recovered."
    assert agent.run.task_state.run_id == captured_run_id


def test_terminal_run_log_starts_new_run(tmp_path):
    agent = Pico(
        FakeModelClient([ModelAction.final("First complete.")]),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        config=PicoConfig(approval_policy="auto", verification_command=""),
    )
    assert agent.ask("First") == "First complete."
    completed_run_id = agent.run.task_state.run_id

    resumed = Pico(
        FakeModelClient([ModelAction.final("Second complete.")]),
        WorkspaceContext.build(tmp_path),
        agent.session.store,
        session=agent.session.store.load(agent.session.data["id"]),
        config=PicoConfig(approval_policy="auto", verification_command=""),
    )

    assert resumed.ask("Second") == "Second complete."
    assert resumed.run.task_state.run_id != completed_run_id


def test_runtime_policy_changes_do_not_invalidate_resume(tmp_path):
    initial = PicoConfig(
        approval_policy="auto",
        max_tool_executions=1,
        max_new_tokens=512,
        read_only=True,
        allowed_tools=("read_file",),
        run_timeout_seconds=10,
        verification_command="",
    )
    agent, store, state, _ = build_interrupted_run(tmp_path, config=initial)
    changed = PicoConfig(
        approval_policy="deny",
        max_tool_executions=50,
        max_new_tokens=2048,
        read_only=False,
        allowed_tools=None,
        run_timeout_seconds=900,
        verification_command="",
    )

    resumed = resume_agent(
        agent,
        store,
        [ModelAction.final("Recovered with new policy.")],
        config=changed,
    )

    assert resumed.recovery.state["status"] == RESUME_READY
    assert resumed.ask("Continue") == "Recovered with new policy."
    assert resumed.run.task_state.run_id == state.run_id


def test_call_persisted_before_tool_start_is_not_replayed(tmp_path):
    agent, store, _, run_log = build_interrupted_run(tmp_path)
    call = ToolCall("write_file", {"path": "x.txt"}, "call_not_started")
    run_log.append_tool_call(call)

    resumed = resume_agent(agent, store, [ModelAction.final("Recovered.")])
    assert resumed.ask("Continue") == "Recovered."

    result = next(
        entry
        for entry in resumed.run.run_log.events
        if entry.kind == "tool_result" and entry.call_id == call.call_id
    )
    assert result.payload["outcome"]["execution_state"] == "not_started"
    assert not (tmp_path / "x.txt").exists()


def test_started_tool_with_unchanged_exact_path_recovers_as_error(tmp_path):
    agent, store, _, run_log = build_interrupted_run(tmp_path)
    call = ToolCall("write_file", {"path": "x.txt"}, "call_unchanged")
    run_log.append_tool_call(call)
    run_log.append_tool_started(
        call,
        risky=True,
        effect_scope="workspace",
        potential_effects=[{"path": "x.txt", "before_state": "absent"}],
    )

    resumed = resume_agent(agent, store, [ModelAction.final("Recovered.")])
    assert resumed.ask("Continue") == "Recovered."
    result = next(
        entry
        for entry in resumed.run.run_log.events
        if entry.kind == "tool_result" and entry.call_id == call.call_id
    )
    assert result.outcome_status == "error"
    assert result.side_effect_state == "none"


def test_started_tool_with_changed_exact_path_recovers_as_partial(tmp_path):
    agent, store, _, run_log = build_interrupted_run(tmp_path)
    call = ToolCall("write_file", {"path": "x.txt"}, "call_changed")
    run_log.append_tool_call(call)
    run_log.append_tool_started(
        call,
        risky=True,
        effect_scope="workspace",
        potential_effects=[{"path": "x.txt", "before_state": "absent"}],
    )
    (tmp_path / "x.txt").write_text("side effect\n", encoding="utf-8")

    resumed = resume_agent(agent, store, [])
    resumed.recovery.evaluate()
    state_run_id = "run_interrupted"
    restored = RunLog.restore(state_run_id, resumed.dependencies.run_store)
    resumed.run.task_state = TaskState.from_dict(
        resumed.dependencies.run_store.replay(state_run_id).task_state()
    )
    resumed.run.run_log = restored
    restored.reconcile_interrupted(resumed)

    result = restored.events[-1]
    assert result.outcome_status == "partial_success"
    assert result.side_effect_state == "partial"
    assert result.affected_paths == ("x.txt",)
