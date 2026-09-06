
import shlex
import sys
import tempfile

import pytest

from pico import (
    FakeModelClient,
    ModelAction,
    Pico,
    PicoConfig,
    SessionStore,
    ToolCall,
    Workspace,
)
from pico.completion_controller import CompletionController
from pico.contracts import ToolOutcome
from pico.execution import ExecutionContext
from pico.mutations import file_revision
from pico.run_lifecycle import RunLifecycle, reconcile_interrupted
from pico.run_log import RunLog
from pico.task_state import TaskContract


def observed_final(answer):
    return [
        ModelAction.tool("list_files", {"path": "."}),
        ModelAction.final(answer),
    ]


def build_interrupted_run(tmp_path, config=None):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    store = SessionStore(tmp_path / ".pico/sessions")
    effective_config = config or PicoConfig(
        mode="auto", verification_command=""
    )
    runtime_workspace = Workspace.build(tmp_path)
    agent = Pico(
        FakeModelClient([]),
        runtime_workspace,
        config=effective_config,
        session=store.create(runtime_workspace.root),
    )
    contract = TaskContract(
        "Inspect",
        allows_workspace_mutation=effective_config.mode != "ask",
        verify_changes=False,
        allowed_write_paths=(
            ()
            if effective_config.mode == "ask"
            else effective_config.allowed_write_paths
        ),
    )
    log = RunLog(
        "run_interrupted",
        "task_interrupted",
        agent.session.id,
        agent.dependencies.run_store,
    )
    log.append_user(contract)
    projection = log.projection
    agent.run.projection = projection
    agent.run.run_log = log
    agent.session.set_active_run(projection.run_id)
    return agent, store, projection, log


def resumed_agent(agent, store, outputs, config=None):
    runtime_workspace = Workspace.build(agent.workspace.root)
    return Pico(
        FakeModelClient(outputs),
        runtime_workspace,
        run_store=agent.dependencies.run_store,
        config=config or PicoConfig(mode="auto", verification_command=""),
        session=store.load(agent.session.id),
    )


def test_active_run_restores_same_projection(tmp_path, monkeypatch):
    agent, store, projection, log = build_interrupted_run(tmp_path)
    agent.run.execution_context = ExecutionContext.root(max_seconds=30)
    call = ToolCall("read_file", {"path": "README.md"}, "read")
    log.append_tool_call(call)
    assert agent.tools.execute_pending(call.call_id).status == "success"
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
    assert resumed.run.run_log is snapshots[0][0]
    assert resumed.run.projection.last_cursor.event_id == snapshots[0][0].events[-1].event_id
    assert resumed.run.resumable is True
    assert resumed.ask("Continue").answer == "Recovered."
    assert resumed.run.projection.run_id == projection.run_id
    assert resumed.run.projection.contract.goal == "Inspect"
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


def test_resume_reuses_persisted_contract(tmp_path):
    agent, store, projection, _log = build_interrupted_run(tmp_path)
    resumed = resumed_agent(agent, store, observed_final("Recovered."))
    outcome = resumed.ask("Continue")

    assert outcome.answer == "Recovered."
    assert resumed.run.projection.contract == projection.contract


def test_resume_keeps_contract_scope_and_applies_current_narrower_policy(tmp_path):
    first_config = PicoConfig(
        mode="auto",
        verification_command="",
        allowed_write_paths=("README.md",),
    )
    agent, store, _projection, _log = build_interrupted_run(
        tmp_path, config=first_config
    )
    narrower_config = PicoConfig(
        mode="auto",
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
            ModelAction.tool("list_files", {"path": "."}),
            ModelAction.final("Recovered."),
        ],
        config=narrower_config,
    )

    assert resumed.ask("Continue").answer == "Recovered."
    assert resumed.run.projection.contract.allowed_write_paths == ("README.md",)
    tool_result = next(
        event for event in resumed.run.run_log.events if event.kind == "tool_result"
    )
    assert tool_result.payload["outcome"]["failure"]["code"] == "invalid_arguments"
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "demo\n"


def test_resume_cannot_widen_original_ask_contract(tmp_path):
    agent, store, _projection, _log = build_interrupted_run(
        tmp_path,
        config=PicoConfig(mode="ask"),
    )
    resumed = resumed_agent(
        agent,
        store,
        [
            ModelAction.tool(
                "write_file",
                {"path": "forbidden.txt", "content": "forbidden\n"},
            ),
            *observed_final("Stayed read-only."),
        ],
        config=PicoConfig(mode="auto"),
    )

    outcome = resumed.ask("Continue in auto mode")

    assert outcome.answer == "Stayed read-only."
    assert not (tmp_path / "forbidden.txt").exists()
    result = next(
        event
        for event in resumed.run.run_log.events
        if event.kind == "tool_result" and event.name == "write_file"
    )
    assert result.payload["outcome"]["failure"]["code"] == "tool_not_allowed"


def test_user_contract_is_durable_before_session_pointer(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / ".pico/sessions")
    runtime_workspace = Workspace.build(tmp_path)
    agent = Pico(
        FakeModelClient([]),
        runtime_workspace,
        config=PicoConfig(mode="auto", verification_command=""),
        session=store.create(runtime_workspace.root),
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
        agent.ask("Persist")
    monkeypatch.setattr(agent.session, "set_active_run", original)
    agent.model_client.outputs.extend(observed_final("Recovered."))
    original_complete = agent.model_client.complete_action

    def complete_with_durable_pointer(*args, **kwargs):
        assert agent.session.active_run_id == captured
        return original_complete(*args, **kwargs)

    monkeypatch.setattr(
        agent.model_client,
        "complete_action",
        complete_with_durable_pointer,
    )
    assert agent.ask("Continue").answer == "Recovered."
    assert agent.run.projection.run_id == captured


@pytest.mark.parametrize("ambiguous_kind", ["user_message", "model_requested"])
def test_same_runtime_reloads_a_durably_committed_ambiguous_append(
    tmp_path,
    monkeypatch,
    ambiguous_kind,
):
    runtime_workspace = Workspace.build(tmp_path)
    agent = Pico(
        FakeModelClient(observed_final("Recovered.")),
        runtime_workspace,
        config=PicoConfig(mode="auto", verification_command=""),
        session=SessionStore(tmp_path / ".pico/sessions").create(
            runtime_workspace.root
        ),
    )
    original_append = agent.dependencies.run_store._append_event
    failed = False

    def commit_then_raise(event):
        nonlocal failed
        original_append(event)
        if event.kind == ambiguous_kind and not failed:
            failed = True
            raise OSError("ambiguous append")

    monkeypatch.setattr(
        agent.dependencies.run_store,
        "_append_event",
        commit_then_raise,
    )
    with pytest.raises(OSError, match="ambiguous append"):
        agent.ask("Inspect")

    run_id = agent.run.projection.run_id
    assert run_id
    assert agent.run.resumable is True
    assert agent.session.active_run_id == run_id
    assert [event.sequence for event in agent.run.run_log.events] == list(
        range(1, len(agent.run.run_log.events) + 1)
    )

    assert agent.ask("Continue").answer == "Recovered."
    replayed = agent.dependencies.run_store.replay(run_id)
    assert agent.run.projection.summary() == replayed.summary()


def test_precommit_first_event_failure_restores_empty_runtime_state(
    tmp_path,
    monkeypatch,
):
    runtime_workspace = Workspace.build(tmp_path)
    agent = Pico(
        FakeModelClient([]),
        runtime_workspace,
        config=PicoConfig(mode="auto", verification_command=""),
        session=SessionStore(tmp_path / ".pico/sessions").create(
            runtime_workspace.root
        ),
    )
    store = agent.dependencies.run_store
    original_append = store._append_event
    failed = False

    def fail_before_commit(event):
        nonlocal failed
        if event.kind == "user_message" and not failed:
            failed = True
            raise OSError("precommit failure")
        return original_append(event)

    monkeypatch.setattr(store, "_append_event", fail_before_commit)
    with pytest.raises(OSError, match="precommit failure"):
        agent.ask("Inspect")

    assert agent.run.projection.contract is None
    assert agent.run.run_log is None
    assert agent.session.active_run_id == ""
    agent.reset()

    agent.model_client.outputs.extend(observed_final("Retried."))
    assert agent.ask("Inspect").answer == "Retried."


def test_same_runtime_reuses_unfinished_run_after_unhandled_model_error(tmp_path):
    runtime_workspace = Workspace.build(tmp_path)
    agent = Pico(
        FakeModelClient([]),
        runtime_workspace,
        config=PicoConfig(mode="auto", verification_command=""),
        session=SessionStore(tmp_path / ".pico/sessions").create(
            runtime_workspace.root
        ),
    )

    with pytest.raises(RuntimeError, match="ran out of outputs"):
        agent.ask("Inspect")

    run_id = agent.run.projection.run_id
    assert agent.run.resumable is True
    agent.model_client.outputs.extend(observed_final("Recovered."))

    assert agent.ask("Continue").answer == "Recovered."
    assert agent.run.projection.run_id == run_id


def test_manual_tool_is_rejected_while_resumable_run_is_dormant(tmp_path):
    agent, _store, _projection, _log = build_interrupted_run(tmp_path)

    outcome = agent.tools.execute_manual("read_file", {"path": "README.md"})

    assert outcome.status == "rejected"
    assert outcome.failure.code == "run_protocol_violation"
    assert "resumed" in outcome.content


def test_terminal_session_pointer_is_cleaned_on_startup(tmp_path):
    runtime_workspace = Workspace.build(tmp_path)
    agent = Pico(
        FakeModelClient(observed_final("Done.")),
        runtime_workspace,
        config=PicoConfig(mode="auto", verification_command=""),
        session=SessionStore(tmp_path / ".pico/sessions").create(
            runtime_workspace.root
        ),
    )
    assert agent.ask("Finish").answer == "Done."
    terminal_run_id = agent.run.projection.run_id
    agent.session.set_active_run(terminal_run_id)

    restarted = resumed_agent(agent, agent.session.store, [])

    assert restarted.session.active_run_id == ""
    assert restarted.run.projection.contract is None
    assert restarted.run.resumable is False


@pytest.mark.parametrize("corruption", ["missing", "malformed"])
def test_corrupt_session_pointer_fails_closed(tmp_path, corruption):
    store = SessionStore(tmp_path / ".pico/sessions")
    runtime_workspace = Workspace.build(tmp_path)
    agent = Pico(
        FakeModelClient([]),
        runtime_workspace,
        config=PicoConfig(mode="auto", verification_command=""),
        session=store.create(runtime_workspace.root),
    )
    run_id = "run_corrupt_pointer"
    agent.session.set_active_run(run_id)
    if corruption == "malformed":
        path = agent.dependencies.run_store.events_path(run_id)
        path.parent.mkdir(parents=True)
        path.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(ValueError):
        resumed_agent(agent, store, [])


def test_persisted_call_without_start_is_not_replayed(tmp_path):
    agent, store, _projection, log = build_interrupted_run(tmp_path)
    call = ToolCall("write_file", {"path": "x.txt", "content": "x\n"}, "write")
    log.append_tool_call(call)
    resumed = resumed_agent(agent, store, observed_final("Recovered."))
    assert resumed.ask("Continue").answer == "Recovered."
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
        effect_scope="workspace",
        potential_effects=[
            {"path": "x.txt", "before_state": "absent", "before_artifact_id": ""}
        ],
    )
    resumed = resumed_agent(agent, store, observed_final("Recovered."))
    assert resumed.ask("Continue").answer == "Recovered."
    result = next(event for event in resumed.run.run_log.events if event.call_id == "write" and event.kind == "tool_result")
    assert result.outcome_status == "error"
    assert result.side_effect_state == "none"


def test_started_changed_path_recovers_as_partial_without_replay(tmp_path):
    agent, store, _projection, log = build_interrupted_run(tmp_path)
    call = ToolCall("write_file", {"path": "x.txt", "content": "x\n"}, "write")
    log.append_tool_call(call)
    log.append_tool_started(
        call,
        effect_scope="workspace",
        potential_effects=[
            {"path": "x.txt", "before_state": "absent", "before_artifact_id": ""}
        ],
    )
    (tmp_path / "x.txt").write_text("side effect\n", encoding="utf-8")
    resumed = resumed_agent(agent, store, [])
    restored, resumed.run.projection = resumed.dependencies.run_store.load_run("run_interrupted")
    resumed.run.run_log = restored
    reconcile_interrupted(resumed)
    result = restored.events[-1]
    assert result.outcome_status == "partial_success"
    assert result.side_effect_state == "partial"
    assert result.affected_paths == ("x.txt",)


@pytest.mark.parametrize("correct", [True, False])
@pytest.mark.parametrize("restart_after_verification", [True, False])
def test_recovered_content_is_verified_without_requiring_another_write(
    tmp_path, correct, restart_after_verification,
):
    check = (
        "from pathlib import Path; scope = {}; "
        "exec(compile(Path('add.py').read_text(), 'add.py', 'exec'), scope); "
        "assert all(scope['add'](a,b) == a+b "
        "for a,b in [(2,3),(-2,3),(0,0),(1.5,2.5)])"
    )
    config = PicoConfig(
        mode="auto", verification_command=shlex.join([sys.executable, "-B", "-c", check]),
    )
    agent, store, _, log = build_interrupted_run(tmp_path, config=config)
    content = "def add(a, b):\n    return a + b\n"
    call = ToolCall("write_file", {"path": "add.py", "content": content}, "write")
    log.append_tool_call(call)
    log.append_tool_started(
        call, effect_scope="workspace",
        potential_effects=[
            {"path": "add.py", "before_state": "absent", "before_artifact_id": ""},
        ],
    )
    target = tmp_path / "add.py"
    target.write_text(content if correct else content.replace("+", "-"))
    resumed = resumed_agent(agent, store, [], config=config)
    reconcile_interrupted(resumed)
    resumed.run.execution_context = ExecutionContext.root(max_seconds=30)
    read = ToolCall("read_file", {"path": "add.py"}, "read")
    resumed.run.run_log.append_tool_call(read)
    assert resumed.tools.execute_pending(read.call_id).status == "success"

    decision = CompletionController(resumed).assess("done")
    assert decision.status == ("allowed" if correct else "verification_failed")
    assert len(resumed.run.evidence.verifications) == 1
    resumed.run.execution_context = None
    actions = [] if correct else [ModelAction.tool(
        "edit_file",
        {"path": "add.py", "old_text": "a - b", "new_text": "a + b",
         "expected_revision": file_revision(target)},
        call_id="repair",
    )]
    actions.append(ModelAction.final("Addition implemented and verified."))
    if restart_after_verification:
        resumed = resumed_agent(resumed, store, actions, config=config)
    else:
        resumed.model_client.outputs = actions
    outcome = resumed.ask("Continue")

    assert outcome.status == "completed"
    assert target.read_text() == content
    assert [record["status"] for record in resumed.run.evidence.verifications] == (
        ["passed", "passed"] if correct else ["failed", "passed"]
    )
    events = resumed.run.run_log.events
    assert sum(event.kind == "tool_started" and event.call_id == "write" for event in events) == 1
    assert sum(event.kind == "tool_started" and event.call_id == "repair" for event in events) == (0 if correct else 1)
    replayed = resumed.dependencies.run_store.replay(outcome.run_id)
    assert replayed.evidence.to_dict() == resumed.run.evidence.to_dict()
    assert replayed.evidence.partial_workspace_effects()[0]["side_effect_state"] == "partial"
    diff = resumed.dependencies.artifacts.read_internal_text(
        outcome.run_id, outcome.final_diff.artifact_id,
    )
    assert "+    return a + b" in diff


@pytest.mark.parametrize("restart", [False, True])
def test_completion_reruns_verification_after_untracked_dependency_changes(tmp_path, restart):
    dependency = tmp_path / "dependency.txt"
    dependency.write_text("2")
    check = (
        "from pathlib import Path; "
        "assert int(Path('result.txt').read_text()) + int(Path('dependency.txt').read_text()) == 3"
    )
    config = PicoConfig(
        mode="auto", verification_command=shlex.join([sys.executable, "-B", "-c", check]),
    )
    store = SessionStore(tmp_path / ".pico/sessions")
    agent = Pico(FakeModelClient([]), Workspace.build(tmp_path),
                 config=config, session=store.create(tmp_path))
    RunLifecycle(agent).initialize("Create result.txt so the sum with dependency.txt is 3")
    call = ToolCall("write_file", {"path": "result.txt", "content": "1"}, "write")
    agent.run.run_log.append_tool_call(call)
    assert agent.tools.execute_pending(call.call_id).status == "success"
    assert CompletionController(agent).assess("done").allowed
    dependency.write_text("9")
    actions = [ModelAction.final("done") for _ in range(3)]
    if restart:
        agent = Pico(FakeModelClient(actions), Workspace.build(tmp_path),
                     config=config, session=store.load(agent.session.id))
    else:
        agent.model_client.outputs = actions
        agent.run.execution_context = None
    outcome = agent.ask("Continue")
    assert outcome.status == "stopped"
    assert agent.run.evidence.touched_paths == ["result.txt"]
    assert [v["status"] for v in agent.run.evidence.verifications] == [
        "passed", "failed", "failed", "failed",
    ]


@pytest.mark.parametrize("suffix", [".txt.", ".json."])
@pytest.mark.parametrize("finish", ["resume", "reset"])
def test_partial_final_artifact_write_does_not_block_recovery(tmp_path, monkeypatch, suffix, finish):
    store = SessionStore(tmp_path / ".pico/sessions")
    agent = Pico(FakeModelClient([
        ModelAction.tool("write_file", {"path": "done.txt", "content": "completed work\n"}),
        ModelAction.final("Done."),
    ]), Workspace.build(tmp_path), config=PicoConfig(mode="auto"),
        session=store.create(tmp_path))
    original_stage = tempfile.NamedTemporaryFile
    failures = []

    def fail_during_write(*args, **kwargs):
        stage = original_stage(*args, **kwargs)
        prefix = kwargs.get("prefix", "")
        if prefix.startswith("diff_") and suffix in prefix:
            write = stage.write

            def partial_write(data):
                write(data[:8])
                stage.flush()
                failures.append(stage.name)
                raise OSError("injected partial artifact write")

            stage.write = partial_write
        return stage

    with monkeypatch.context() as fault:
        fault.setattr("pico.persistence.tempfile.NamedTemporaryFile", fail_during_write)
        with pytest.raises(OSError, match="partial artifact write"):
            agent.ask("Create done.txt")
    assert len(failures) == 1
    run_id = agent.run.projection.run_id
    assert not agent.run.projection.terminal
    resumed = Pico(FakeModelClient([ModelAction.final("Done.")]), Workspace.build(tmp_path),
                   config=agent.config, session=store.load(agent.session.id))
    if finish == "resume":
        assert resumed.ask("Continue").status == "completed"
    else:
        resumed.reset()
    replayed = resumed.dependencies.run_store.replay(run_id)
    assert replayed.status == ("completed" if finish == "resume" else "stopped")
    assert store.load(agent.session.id).active_run_id == ""
    diff = resumed.dependencies.artifacts.read_internal_text(run_id, replayed.final_diff.artifact_id)
    assert "+completed work" in diff
    assert not list(resumed.dependencies.run_store.artifact_dir(run_id).glob("*.tmp"))


def test_reused_call_id_is_scoped_to_the_pending_transaction(tmp_path):
    agent, store, _, log = build_interrupted_run(tmp_path)
    agent.run.execution_context = ExecutionContext.root(max_seconds=30)
    call = ToolCall("read_file", {"path": "README.md"}, "same")
    log.append_tool_call(call)
    assert agent.tools.execute_pending("same").status == "success"
    log.append_tool_call(call)
    resumed = resumed_agent(agent, store, [ModelAction.final("Recovered.")])
    assert resumed.ask("Continue").status == "completed"
    recovered = [e for e in resumed.run.run_log.events if e.payload.get("recovered_from_interruption")]
    assert len(recovered) == 1
    assert recovered[0].payload["outcome"]["execution_state"] == "not_started"


@pytest.mark.parametrize("batch", [False, True])
def test_resumed_request_receives_a_new_tool_budget(tmp_path, batch):
    count = 2 if batch else 1
    config = PicoConfig(mode="auto", max_tool_executions=count)
    runtime_workspace = Workspace.build(tmp_path)
    store = SessionStore(tmp_path / ".pico/sessions")
    action = (ModelAction.tool_batch((ToolCall("list_files", {"path": "."}, "one"),
                                     ToolCall("list_files", {"path": "."}, "two")))
              if batch else ModelAction.tool("list_files", {"path": "."}))
    agent = Pico(FakeModelClient([action]),
                 runtime_workspace, config=config, session=store.create(tmp_path))
    with pytest.raises(RuntimeError, match="ran out of outputs"):
        agent.ask("Inspect")
    resumed = resumed_agent(agent, store, [
        action, ModelAction.final("done"),
    ], config=config)
    assert resumed.ask("Inspect again").status == "completed"
    assert "list_files" in resumed.model_client.action_tool_surfaces[0]
    assert resumed.model_client.action_tool_surfaces[-1] == ("submit_final",)
    assert resumed.run.metrics.executed_tool_count == count * 2


def test_interrupted_multi_file_effect_can_be_repaired_by_separate_edits(tmp_path):
    check = (
        "from pathlib import Path; "
        "assert all(Path(p).read_text() == 'after' for p in ['a.txt', 'b.txt'])"
    )
    config = PicoConfig(
        mode="auto", verification_command=shlex.join([sys.executable, "-B", "-c", check]),
    )
    agent, store, _, log = build_interrupted_run(tmp_path, config=config)
    # Inject the durable prefix and file effects of an interrupted integration;
    # recovery must never invoke the original integrate_child call again.
    call = ToolCall("integrate_child", {"child_id": "interrupted_child"}, "integrate")
    log.append_tool_call(call)
    log.append_tool_started(
        call, effect_scope="workspace",
        potential_effects=[
            {"path": path, "before_state": "absent", "before_artifact_id": ""}
            for path in ("a.txt", "b.txt")
        ],
    )
    actions = []
    for path in ("a.txt", "b.txt"):
        target = tmp_path / path
        target.write_text("before")
        actions.append(ModelAction.tool(
            "edit_file",
            {"path": path, "old_text": "before", "new_text": "after",
             "expected_revision": file_revision(target)},
            call_id=f"repair_{path}",
        ))
    actions.append(ModelAction.final("Both files repaired and verified."))
    resumed = resumed_agent(agent, store, actions, config=config)
    outcome = resumed.ask("Continue")
    assert outcome.status == "completed"
    assert resumed.run.evidence.partial_workspace_effects()[0]["affected_paths"] == ("a.txt", "b.txt")
    assert [item["status"] for item in resumed.run.evidence.verifications] == ["passed"]
    edits = [item for item in resumed.run.evidence.effects if item["status"] == "success"]
    assert [item["affected_paths"] for item in edits] == [("a.txt",), ("b.txt",)]


def test_unstarted_observation_batch_recovers_every_call_without_replay(tmp_path):
    agent, store, _projection, log = build_interrupted_run(tmp_path)
    calls = (
        ToolCall("read_file", {"path": "README.md"}, "call_a"),
        ToolCall("search", {"pattern": "demo", "path": "."}, "call_b"),
    )
    log.append_tool_batch(calls)

    resumed = resumed_agent(agent, store, observed_final("Recovered."))
    outcome = resumed.ask("Continue")

    assert outcome.answer == "Recovered."
    results = [
        event
        for event in resumed.run.run_log.events
        if event.kind == "tool_result" and event.call_id in {"call_a", "call_b"}
    ]
    assert [event.call_id for event in results] == ["call_a", "call_b"]
    assert all(
        event.payload["outcome"]["execution_state"] == "not_started"
        for event in results
    )
    assert all(event.side_effect_state == "none" for event in results)
    assert resumed.run.projection.pending_call_ids == ()


def test_observation_batch_recovery_keeps_result_prefix_and_closes_suffix(tmp_path):
    agent, store, _projection, log = build_interrupted_run(tmp_path)
    calls = (
        ToolCall("read_file", {"path": "README.md"}, "call_a"),
        ToolCall("search", {"pattern": "demo", "path": "."}, "call_b"),
    )
    log.append_tool_batch(calls)
    for call in calls:
        log.append_tool_started(call, effect_scope="none", potential_effects=[])
    log.append_tool_result(
        ToolOutcome(
            "call_a",
            "read_file",
            "success",
            "completed",
            "none",
            "already durable",
        )
    )

    resumed = resumed_agent(agent, store, observed_final("Recovered."))
    resumed.ask("Continue")

    results = [
        event
        for event in resumed.run.run_log.events
        if event.kind == "tool_result" and event.call_id in {"call_a", "call_b"}
    ]
    assert [event.call_id for event in results] == ["call_a", "call_b"]
    assert results[0].content == "already durable"
    assert results[1].payload["outcome"]["failure"]["code"] == (
        "operation_interrupted"
    )
    assert results[1].side_effect_state == "none"
