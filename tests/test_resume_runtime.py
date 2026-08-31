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
from pico.execution import ExecutionContext
from pico.mutations import file_revision
from pico.run_lifecycle import load_resumable_run
from pico.run_log import RunLog
from pico.run_projection import RunProjection
from pico.runtime_state import ActiveRunState
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
    effective_config = config or PicoConfig(
        approval_policy="auto", verification_command=""
    )
    agent = Pico(
        FakeModelClient([]),
        WorkspaceContext.build(tmp_path),
        store,
        config=effective_config,
    )
    contract = TaskContract(
        "Inspect",
        **NO_CHANGE_TASK,
        allowed_write_paths=effective_config.allowed_write_paths,
    )
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


def test_active_run_restores_same_projection(tmp_path, monkeypatch):
    agent, store, projection, log = build_interrupted_run(tmp_path)
    agent.run.execution_context = ExecutionContext.root(max_seconds=30)
    call = ToolCall("read_file", {"path": "README.md"}, "read")
    agent.apply_run_event(log.append_tool_call(call))
    assert agent.tools.execute(call).status == "success"
    agent.session.set_active_run("")

    snapshots = []
    original_load_run = agent.dependencies.run_store.load_run

    def capture_snapshot(run_id):
        snapshot = original_load_run(run_id)
        snapshots.append(snapshot)
        return snapshot

    monkeypatch.setattr(agent.dependencies.run_store, "load_run", capture_snapshot)

    resumed = resumed_agent(agent, store, [ModelAction.final("Recovered.")])
    assert len(snapshots) == 1
    assert resumed.run.projection is snapshots[0][1]
    assert resumed.run.run_log.events == snapshots[0][0]
    assert resumed.run.projection.last_cursor.event_id == snapshots[0][0][-1].event_id
    assert resumed.run.resumable is True
    assert resumed.ask("Continue", **NO_CHANGE_TASK).answer == "Recovered."
    assert resumed.run.projection.run_id == projection.run_id
    assert resumed.run.task.contract.goal == "Inspect"
    assert resumed.run.projection.pending_call_id is None
    prompt = resumed.model_client.prompts[0]
    assert prompt.count('latest_user_request:\n"Continue"') == 1
    assert "Resume request: Continue" not in prompt
    assert any(
        event.kind == "user_guidance" and event.content == "Continue"
        for event in resumed.run.run_log.events
    )
    assert sum(
        event.kind == "run_resumed" for event in resumed.run.run_log.events
    ) == 1
    assert not any(
        event.kind == "model_instruction"
        for event in resumed.run.run_log.events
    )


def test_resume_guidance_survives_another_process_failure(tmp_path):
    agent, store, _projection, _log = build_interrupted_run(tmp_path)
    first_resume = resumed_agent(agent, store, [])

    with pytest.raises(RuntimeError, match="fake model ran out of outputs"):
        first_resume.ask(
            "Continue, but never touch config.py",
            **NO_CHANGE_TASK,
        )

    second_resume = resumed_agent(
        first_resume,
        store,
        [ModelAction.final("Recovered.")],
    )
    assert second_resume.ask("Continue", **NO_CHANGE_TASK).answer == "Recovered."
    prompt = second_resume.model_client.prompts[0]
    assert "never touch config.py" in prompt
    assert [
        event.content
        for event in second_resume.run.run_log.events
        if event.kind == "user_guidance"
    ] == ["Continue, but never touch config.py", "Continue"]


def test_incremental_working_state_restores_from_tool_events(tmp_path):
    agent, store, projection, log = build_interrupted_run(tmp_path)
    agent.run.execution_context = ExecutionContext.root(max_seconds=30)
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
    assert resumed.ask("Continue", **NO_CHANGE_TASK).answer == "Recovered."
    assert resumed.run.task.contract.goal == projection.task.contract.goal
    assert resumed.run.task.working.constraints == ("Keep schema",)
    assert resumed.run.task.working.decisions == ("Fix refresh",)
    assert resumed.run.task.working.next_steps == ("Add test",)


def test_resume_rejects_requirement_change_without_consuming_recovery(tmp_path):
    agent, store, _projection, _log = build_interrupted_run(tmp_path)
    resumed = resumed_agent(agent, store, [ModelAction.final("Recovered.")])
    run_id = resumed.run.projection.run_id
    before_summary = resumed.run.projection.summary()
    before_events = tuple(event.to_dict() for event in resumed.run.run_log.events)

    with pytest.raises(ValueError, match="do not match"):
        resumed.ask("Continue", **READ_TASK)

    assert resumed.run.resumable is True
    assert resumed.run.projection.summary() == before_summary
    assert tuple(event.to_dict() for event in resumed.run.run_log.events) == before_events

    with pytest.raises(TypeError, match="must be a boolean"):
        resumed.ask(
            "Continue",
            task_kind="modify",
            requires_workspace_change=0,
            requires_verification=False,
        )

    assert resumed.run.projection.summary() == before_summary
    assert tuple(event.to_dict() for event in resumed.run.run_log.events) == before_events

    resumed.reset()
    terminal = resumed.dependencies.run_store.replay(run_id)
    found_run_id, _events, _projection = resumed.dependencies.run_store.find_active_run(
        resumed.session.data["id"]
    )
    assert terminal.terminal is True
    assert terminal.stop_reason == "user_reset"
    assert resumed.session.data["active_run_id"] == ""
    assert found_run_id == ""


def test_resume_keeps_contract_scope_and_applies_current_narrower_policy(tmp_path):
    first_config = PicoConfig(
        approval_policy="auto",
        verification_command="",
        allowed_write_paths=("README.md",),
    )
    agent, store, _projection, _log = build_interrupted_run(
        tmp_path, config=first_config
    )
    narrower_config = PicoConfig(
        approval_policy="auto",
        verification_command="",
        allowed_write_paths=(),
    )
    resumed = resumed_agent(
        agent,
        store,
        [
            ModelAction.tool(
                "edit_file",
                {
                    "path": "README.md",
                    "old_text": "demo",
                    "new_text": "changed",
                    "expected_revision": file_revision(tmp_path / "README.md"),
                },
            ),
            ModelAction.final("Recovered."),
        ],
        config=narrower_config,
    )

    assert resumed.ask("Continue", **NO_CHANGE_TASK).answer == "Recovered."
    assert resumed.run.task.contract.allowed_write_paths == ("README.md",)
    tool_result = next(
        event for event in resumed.run.run_log.events if event.kind == "tool_result"
    )
    assert tool_result.payload["outcome"]["failure"]["code"] == "invalid_arguments"
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "demo\n"


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
    original_complete = agent.model_client.complete_action

    def complete_with_durable_pointer(*args, **kwargs):
        assert agent.session.data["active_run_id"] == captured
        return original_complete(*args, **kwargs)

    monkeypatch.setattr(
        agent.model_client,
        "complete_action",
        complete_with_durable_pointer,
    )
    assert agent.ask("Continue", **NO_CHANGE_TASK).answer == "Recovered."
    assert agent.run.projection.run_id == captured


def test_pointer_repair_failure_does_not_consume_dormant_run(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / ".pico/sessions")
    agent = Pico(
        FakeModelClient([]),
        WorkspaceContext.build(tmp_path),
        store,
        config=PicoConfig(approval_policy="auto", verification_command=""),
    )

    monkeypatch.setattr(
        agent.session,
        "set_active_run",
        lambda _run_id: (_ for _ in ()).throw(OSError("pointer failed")),
    )
    with pytest.raises(OSError, match="pointer failed"):
        agent.ask("Persist", **NO_CHANGE_TASK)
    before = tuple(event.to_dict() for event in agent.run.run_log.events)
    agent.model_client.outputs.append(ModelAction.final("must not run"))

    with pytest.raises(OSError, match="pointer failed"):
        agent.ask("Continue", **NO_CHANGE_TASK)

    assert agent.run.resumable is True
    assert tuple(event.to_dict() for event in agent.run.run_log.events) == before
    assert agent.model_client.prompts == []


@pytest.mark.parametrize("ambiguous_kind", ["user_message", "model_requested"])
def test_same_runtime_reloads_a_durably_committed_ambiguous_append(
    tmp_path,
    monkeypatch,
    ambiguous_kind,
):
    agent = Pico(
        FakeModelClient([ModelAction.final("Recovered.")]),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        config=PicoConfig(approval_policy="auto", verification_command=""),
    )
    original_append = agent.dependencies.run_store.append_event
    failed = False

    def commit_then_raise(*args, **kwargs):
        nonlocal failed
        event = original_append(*args, **kwargs)
        kind = str(args[3]) if len(args) > 3 else str(kwargs.get("kind", ""))
        if kind == ambiguous_kind and not failed:
            failed = True
            raise OSError("ambiguous append")
        return event

    monkeypatch.setattr(
        agent.dependencies.run_store,
        "append_event",
        commit_then_raise,
    )
    with pytest.raises(OSError, match="ambiguous append"):
        agent.ask("Inspect", **NO_CHANGE_TASK)

    run_id = agent.run.projection.run_id
    assert run_id
    assert agent.run.resumable is True
    assert agent.session.data["active_run_id"] == run_id
    assert [event.sequence for event in agent.run.run_log.events] == list(
        range(1, len(agent.run.run_log.events) + 1)
    )

    assert agent.ask("Continue", **NO_CHANGE_TASK).answer == "Recovered."
    replayed = agent.dependencies.run_store.replay(run_id)
    assert agent.run.projection.summary() == replayed.summary()


def test_ambiguous_append_retries_reload_after_transient_load_failure(
    tmp_path,
    monkeypatch,
):
    agent = Pico(
        FakeModelClient([ModelAction.final("Recovered.")]),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        config=PicoConfig(approval_policy="auto", verification_command=""),
    )
    store = agent.dependencies.run_store
    original_append = store.append_event
    original_load = store.load_run
    append_failed = False
    load_failed = False

    def commit_then_raise(*args, **kwargs):
        nonlocal append_failed
        event = original_append(*args, **kwargs)
        kind = str(args[3]) if len(args) > 3 else str(kwargs.get("kind", ""))
        if kind == "model_requested" and not append_failed:
            append_failed = True
            raise OSError("ambiguous append")
        return event

    def fail_first_reload(run_id):
        nonlocal load_failed
        if not load_failed:
            load_failed = True
            raise OSError("reload unavailable")
        return original_load(run_id)

    monkeypatch.setattr(store, "append_event", commit_then_raise)
    monkeypatch.setattr(store, "load_run", fail_first_reload)

    with pytest.raises(OSError, match="reload unavailable"):
        agent.ask("Inspect", **NO_CHANGE_TASK)

    run_id = agent.run.projection.run_id
    assert agent.run.reload_required is True
    assert agent.run.resumable is True

    assert agent.ask("Continue", **NO_CHANGE_TASK).answer == "Recovered."
    replayed = original_load(run_id)[1]
    assert agent.run.reload_required is False
    assert agent.run.projection.summary() == replayed.summary()


def test_unloaded_first_event_blocks_all_manual_tools_until_reset(
    tmp_path,
    monkeypatch,
):
    agent = Pico(
        FakeModelClient([]),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        config=PicoConfig(approval_policy="auto", verification_command=""),
    )
    store = agent.dependencies.run_store
    original_append = store.append_event
    original_load = store.load_run
    append_failed = False
    load_failures = 0

    def commit_then_raise(*args, **kwargs):
        nonlocal append_failed
        event = original_append(*args, **kwargs)
        kind = str(args[3]) if len(args) > 3 else str(kwargs.get("kind", ""))
        if kind == "user_message" and not append_failed:
            append_failed = True
            raise OSError("ambiguous first event")
        return event

    def fail_first_load(run_id):
        nonlocal load_failures
        if load_failures < 2:
            load_failures += 1
            raise OSError("load unavailable")
        return original_load(run_id)

    monkeypatch.setattr(store, "append_event", commit_then_raise)
    monkeypatch.setattr(store, "load_run", fail_first_load)

    with pytest.raises(OSError, match="load unavailable"):
        agent.ask("Inspect", **NO_CHANGE_TASK)

    assert agent.run.task is None
    assert agent.run.reload_required is True
    observed = agent.tools.execute(
        ToolCall("read_file", {"path": "README.md"}, "manual-read")
    )
    mutated = agent.tools.execute(
        ToolCall(
            "write_file",
            {"path": "blocked.txt", "content": "blocked\n"},
            "manual-write",
        )
    )
    assert observed.failure.code == "run_protocol_violation"
    assert mutated.failure.code == "run_protocol_violation"
    assert not (tmp_path / "blocked.txt").exists()

    agent.reset()
    assert agent.run.task is None
    assert agent.session.data["active_run_id"] == ""


def test_precommit_first_event_failure_restores_empty_runtime_state(
    tmp_path,
    monkeypatch,
):
    agent = Pico(
        FakeModelClient([]),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        config=PicoConfig(approval_policy="auto", verification_command=""),
    )
    store = agent.dependencies.run_store
    original_append = store.append_event
    failed = False

    def fail_before_commit(*args, **kwargs):
        nonlocal failed
        kind = str(args[3]) if len(args) > 3 else str(kwargs.get("kind", ""))
        if kind == "user_message" and not failed:
            failed = True
            raise OSError("precommit failure")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(store, "append_event", fail_before_commit)
    with pytest.raises(OSError, match="precommit failure"):
        agent.ask("Inspect", **NO_CHANGE_TASK)

    assert agent.run.task is None
    assert agent.run.run_log is None
    assert agent.run.reload_required is False
    assert agent.session.data["active_run_id"] == ""
    agent.reset()

    agent.model_client.outputs.append(ModelAction.final("Retried."))
    assert agent.ask("Inspect", **NO_CHANGE_TASK).answer == "Retried."


def test_same_runtime_reuses_unfinished_run_after_unhandled_model_error(tmp_path):
    agent = Pico(
        FakeModelClient([]),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        config=PicoConfig(approval_policy="auto", verification_command=""),
    )

    with pytest.raises(RuntimeError, match="ran out of outputs"):
        agent.ask("Inspect", **NO_CHANGE_TASK)

    run_id = agent.run.projection.run_id
    assert agent.run.resumable is True
    agent.model_client.outputs.append(ModelAction.final("Recovered."))

    assert agent.ask("Continue", **NO_CHANGE_TASK).answer == "Recovered."
    assert agent.run.projection.run_id == run_id


def test_manual_tool_is_rejected_while_resumable_run_is_dormant(tmp_path):
    agent, _store, _projection, _log = build_interrupted_run(tmp_path)

    outcome = agent.tools.execute(
        ToolCall("read_file", {"path": "README.md"}, "manual-read")
    )

    assert outcome.status == "rejected"
    assert outcome.failure.code == "run_protocol_violation"
    assert "resumed or reset" in outcome.content


def test_terminal_session_pointer_is_cleaned_on_startup(tmp_path):
    agent = Pico(
        FakeModelClient([ModelAction.final("Done.")]),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        config=PicoConfig(approval_policy="auto", verification_command=""),
    )
    assert agent.ask("Finish", **NO_CHANGE_TASK).answer == "Done."
    terminal_run_id = agent.run.projection.run_id
    agent.session.set_active_run(terminal_run_id)

    restarted = resumed_agent(agent, agent.session.store, [])

    assert restarted.session.data["active_run_id"] == ""
    assert restarted.run.task is None
    assert restarted.run.resumable is False


def test_terminal_candidate_clears_temporary_reload_state(tmp_path):
    agent = Pico(
        FakeModelClient([ModelAction.final("Done.")]),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        config=PicoConfig(approval_policy="auto", verification_command=""),
    )
    assert agent.ask("Finish", **NO_CHANGE_TASK).answer == "Done."
    terminal_log = agent.run.run_log
    agent.run = ActiveRunState(run_log=terminal_log, reload_required=True)

    load_resumable_run(agent)

    assert agent.run.task is None
    assert agent.run.run_log is None
    assert agent.run.reload_required is False


@pytest.mark.parametrize("corruption", ["missing", "malformed"])
def test_corrupt_session_pointer_fails_closed(tmp_path, corruption):
    store = SessionStore(tmp_path / ".pico/sessions")
    agent = Pico(
        FakeModelClient([]),
        WorkspaceContext.build(tmp_path),
        store,
        config=PicoConfig(approval_policy="auto", verification_command=""),
    )
    run_id = "run_corrupt_pointer"
    agent.session.set_active_run(run_id)
    if corruption == "malformed":
        path = agent.dependencies.run_store.events_path(run_id)
        path.parent.mkdir(parents=True)
        path.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(ValueError):
        resumed_agent(agent, store, [])


def test_terminal_run_starts_a_new_run(tmp_path):
    agent = Pico(
        FakeModelClient([ModelAction.final("First.")]),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        config=PicoConfig(approval_policy="auto", verification_command=""),
    )
    assert agent.ask("First", **NO_CHANGE_TASK).answer == "First."
    first_run = agent.run.projection.run_id
    resumed = resumed_agent(agent, agent.session.store, [ModelAction.final("Second.")])
    assert resumed.ask("Second", **NO_CHANGE_TASK).answer == "Second."
    assert resumed.run.projection.run_id != first_run


def test_persisted_call_without_start_is_not_replayed(tmp_path):
    agent, store, _projection, log = build_interrupted_run(tmp_path)
    call = ToolCall("write_file", {"path": "x.txt", "content": "x\n"}, "write")
    log.append_tool_call(call)
    resumed = resumed_agent(agent, store, [ModelAction.final("Recovered.")])
    assert resumed.ask("Continue", **NO_CHANGE_TASK).answer == "Recovered."
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
    assert resumed.ask("Continue", **NO_CHANGE_TASK).answer == "Recovered."
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
