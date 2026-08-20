from pico import (
    FakeModelClient,
    ModelAction,
    Pico,
    PicoConfig,
    SessionStore,
    ToolCall,
    WorkspaceContext,
)
from pico.run_journal import RunJournal
from pico.runtime_recovery import RESUME_READY
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
    agent.services.run_store.start_run(state)
    journal = RunJournal(
        state.run_id,
        state.task_id,
        agent.session.data["id"],
        agent.services.run_store,
    )
    journal.append_user("Inspect")
    agent.run.journal = journal
    agent.session.data["active_run_id"] = state.run_id
    agent.session.save()
    return agent, store, state, journal


def resume_agent(agent, store, outputs, *, config=None):
    return Pico(
        FakeModelClient(outputs),
        WorkspaceContext.build(agent.workspace.root),
        store,
        session=store.load(agent.session.data["id"]),
        run_store=agent.services.run_store,
        config=config or PicoConfig(approval_policy="auto", verification_command=""),
    )


def test_active_journal_restores_same_run(tmp_path):
    agent, store, state, journal = build_interrupted_run(tmp_path)
    call = ToolCall("read_file", {"path": "README.md"}, "call_read")
    journal.append_tool_call(call)
    agent.tools.run(call)
    agent.session.data["active_run_id"] = ""
    agent.session.save()

    run_store = agent.services.run_store
    original_read_entries = run_store.read_entries
    read_count = 0

    def counted_read_entries(run_id):
        nonlocal read_count
        read_count += 1
        return original_read_entries(run_id)

    run_store.read_entries = counted_read_entries

    resumed = resume_agent(agent, store, [ModelAction.final("Recovered.")])

    assert resumed.recovery.state["status"] == RESUME_READY
    assert resumed.ask("Continue") == "Recovered."
    assert resumed.run.task_state.run_id == state.run_id
    assert resumed.run.task_state.user_request == "Inspect"
    assert "Inspect" in resumed.session.memory.render_panel()
    assert "Continue" not in resumed.session.memory.render_panel()
    assert read_count == 1


def test_terminal_journal_starts_new_run(tmp_path):
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
        max_steps=1,
        max_new_tokens=512,
        read_only=True,
        allowed_tools=("read_file",),
        run_timeout_seconds=10,
        verification_command="",
    )
    agent, store, state, _ = build_interrupted_run(tmp_path, config=initial)
    changed = PicoConfig(
        approval_policy="never",
        max_steps=50,
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
    agent, store, _, journal = build_interrupted_run(tmp_path)
    call = ToolCall("write_file", {"path": "x.txt"}, "call_not_started")
    journal.append_tool_call(call)

    resumed = resume_agent(agent, store, [ModelAction.final("Recovered.")])
    assert resumed.ask("Continue") == "Recovered."

    result = next(
        entry
        for entry in resumed.run.journal.entries
        if entry.kind == "tool_result" and entry.call_id == call.call_id
    )
    assert result.payload["outcome"]["execution_state"] == "not_started"
    assert not (tmp_path / "x.txt").exists()


def test_started_tool_with_unchanged_exact_path_recovers_as_error(tmp_path):
    agent, store, _, journal = build_interrupted_run(tmp_path)
    call = ToolCall("write_file", {"path": "x.txt"}, "call_unchanged")
    journal.append_tool_call(call)
    journal.append(
        "tool_started",
        {
            "tool_call_id": call.call_id,
            "tool_name": call.name,
            "effect_scope": "workspace",
            "potential_effects": [{"path": "x.txt", "before_state": "absent"}],
        },
    )

    resumed = resume_agent(agent, store, [ModelAction.final("Recovered.")])
    assert resumed.ask("Continue") == "Recovered."
    result = next(
        entry
        for entry in resumed.run.journal.entries
        if entry.kind == "tool_result" and entry.call_id == call.call_id
    )
    assert result.outcome_status == "error"
    assert result.side_effect_state == "none"


def test_started_tool_with_changed_exact_path_recovers_as_partial(tmp_path):
    agent, store, _, journal = build_interrupted_run(tmp_path)
    call = ToolCall("write_file", {"path": "x.txt"}, "call_changed")
    journal.append_tool_call(call)
    journal.append(
        "tool_started",
        {
            "tool_call_id": call.call_id,
            "tool_name": call.name,
            "effect_scope": "workspace",
            "potential_effects": [{"path": "x.txt", "before_state": "absent"}],
        },
    )
    (tmp_path / "x.txt").write_text("side effect\n", encoding="utf-8")

    resumed = resume_agent(agent, store, [])
    resumed.recovery.evaluate()
    state_run_id = "run_interrupted"
    restored = RunJournal.restore(state_run_id, resumed.services.run_store)
    resumed.run.task_state = TaskState.from_dict(
        resumed.services.run_store.replay(state_run_id).task_state()
    )
    resumed.run.journal = restored
    restored.reconcile_interrupted(resumed)

    result = restored.entries[-1]
    assert result.outcome_status == "partial_success"
    assert result.side_effect_state == "partial"
    assert result.affected_paths == ("x.txt",)
