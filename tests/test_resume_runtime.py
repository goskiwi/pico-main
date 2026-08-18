from pico import (
    FakeModelClient,
    ModelAction,
    Pico,
    SessionStore,
    ToolCall,
    WorkspaceContext,
)
from pico.context_ledger import ContextLedger
from pico.task_state import TaskState


def test_valid_checkpoint_restores_same_run(tmp_path):
    (tmp_path / "README.md").write_text("demo\n")
    store = SessionStore(tmp_path / ".pico/sessions")
    agent = Pico(FakeModelClient([]), WorkspaceContext.build(tmp_path), store,
                 approval_policy="auto", verification_command="")
    state = TaskState.create("task_interrupted", "Inspect", run_id="run_interrupted")
    agent.current_task_state = state
    agent.run_store.start_run(state)
    ledger = ContextLedger(state.run_id, agent.run_store)
    ledger.append_user("Inspect")
    agent.context_ledger = ledger
    call = ToolCall("read_file", {"path": "README.md"}, "call_read")
    ledger.append_tool_call(call)
    ledger.append_tool_result(agent.run_tool(call))
    agent.create_checkpoint(state, "Inspect", "tool_executed")

    resumed = Pico.from_session(
        FakeModelClient([ModelAction.final("Recovered.")]), WorkspaceContext.build(tmp_path),
        store, agent.session["id"], approval_policy="auto", verification_command="",
    )
    assert resumed.ask("Continue") == "Recovered."
    assert resumed.current_task_state.run_id == "run_interrupted"


def test_finished_tool_after_old_checkpoint_is_rebuilt_from_events(tmp_path):
    (tmp_path / "README.md").write_text("demo\n")
    store = SessionStore(tmp_path / ".pico/sessions")
    agent = Pico(
        FakeModelClient([]),
        WorkspaceContext.build(tmp_path),
        store,
        approval_policy="auto",
        verification_command="",
    )
    state = TaskState.create("task_event_tail", "Inspect", run_id="run_event_tail")
    agent.current_task_state = state
    agent.run_store.start_run(state)
    ledger = ContextLedger(state.run_id, agent.run_store)
    ledger.append_user("Inspect")
    agent.context_ledger = ledger
    agent.create_checkpoint(state, "Inspect", "before_tool")

    call = ToolCall("read_file", {"path": "README.md"}, "call_after_checkpoint")
    ledger.append_tool_call(call)
    ledger.append_tool_result(agent.run_tool(call))
    state.record_tool("read_file")

    resumed = Pico.from_session(
        FakeModelClient([ModelAction.final("Recovered from events.")]),
        WorkspaceContext.build(tmp_path),
        store,
        agent.session["id"],
        approval_policy="auto",
        verification_command="",
    )

    assert resumed.ask("Continue") == "Recovered from events."
    assert resumed.current_task_state.run_id == state.run_id
    assert resumed.current_task_state.tool_steps == 1
    assert resumed.run_store.replay(state.run_id).summary()["tool_counts"] == {
        "read_file": 1
    }


def test_pending_operation_is_reconciled_not_replayed(tmp_path):
    (tmp_path / "README.md").write_text("demo\n")
    agent = Pico(FakeModelClient([]), WorkspaceContext.build(tmp_path),
                 SessionStore(tmp_path / ".pico/sessions"), approval_policy="auto",
                 verification_command="")
    state = TaskState.create("task_pending", "Mutate", run_id="run_pending")
    agent.run_store.start_run(state)
    ledger = ContextLedger(state.run_id, agent.run_store)
    ledger.append_user("Mutate")
    call = ToolCall("write_file", {"path": "x.txt"}, "call_pending")
    ledger.append_tool_call(call)
    agent.run_store.append_event(
        state.run_id,
        state.task_id,
        "operation_started",
        {"tool_call_id": call.call_id, "tool_name": call.name},
        correlation_id=call.call_id,
    )
    restored = ContextLedger.restore(state.run_id, agent.run_store)
    assert restored.pending_call_id() == ""
    assert restored.entries[-1].side_effect_state == "unknown"
    assert len(restored.reconciled_outcomes) == 1
    assert restored.reconciled_outcomes[0].failure.code == "operation_interrupted"


def test_agent_resume_writes_terminal_event_for_interrupted_operation(tmp_path):
    (tmp_path / "README.md").write_text("demo\n")
    store = SessionStore(tmp_path / ".pico/sessions")
    agent = Pico(
        FakeModelClient([]),
        WorkspaceContext.build(tmp_path),
        store,
        approval_policy="auto",
        verification_command="",
    )
    state = TaskState.create("task_pending_resume", "Create x", run_id="run_pending_resume")
    agent.current_task_state = state
    agent.run_store.start_run(state)
    ledger = ContextLedger(state.run_id, agent.run_store)
    ledger.append_user("Create x")
    agent.context_ledger = ledger
    agent.create_checkpoint(state, "Create x", "before_interruption")
    interrupted = ToolCall("write_file", {"path": "x.txt"}, "call_interrupted")
    ledger.append_tool_call(interrupted)
    agent.run_store.append_event(
        state.run_id,
        state.task_id,
        "operation_started",
        {"tool_call_id": interrupted.call_id, "tool_name": interrupted.name},
        correlation_id=interrupted.call_id,
    )

    resumed = Pico.from_session(
        FakeModelClient(
            [
                ModelAction.tool(
                    "write_file",
                    {"path": "x.txt", "content": "recovered\n", "expected_revision": "absent"},
                ),
                ModelAction.final("Recovered."),
            ]
        ),
        WorkspaceContext.build(tmp_path),
        store,
        agent.session["id"],
        approval_policy="auto",
        verification_command="",
    )

    assert resumed.ask("Continue") == "Recovered."
    projection = resumed.run_store.replay(state.run_id)
    receipt = projection.operation_receipt(interrupted.call_id)
    assert projection.summary()["pending_operations"] == []
    assert receipt["state"] == "finished"
    assert receipt["outcome"]["side_effect_state"] == "unknown"
    assert receipt["recovered_from_interruption"] is True
    assert resumed.current_task_state.tool_steps == 2
