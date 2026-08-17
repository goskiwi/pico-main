import pytest

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.context_ledger import ContextLedger
from pico.context_manager import ContextBudgetExceeded, ContextManager
from pico.contracts import ToolCall, ToolOutcome
from pico.task_state import TaskState


def build_agent(tmp_path, max_new_tokens=64):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
        max_new_tokens=max_new_tokens,
    )


def test_context_uses_token_budgets_and_preserves_request(tmp_path):
    agent = build_agent(tmp_path)
    agent.memory.set_goal("deploy key is red")
    prompt, metadata = ContextManager(agent, total_budget=900).build("Where is the deploy key?")

    assert prompt.rstrip().endswith("Current user request:\nWhere is the deploy key?")
    assert metadata["within_budget"] is True
    assert metadata["prompt_tokens"] + metadata["reserved_output_tokens"] <= 900
    assert metadata["tokenizer"]
    assert metadata["section_order"] == [
        "prefix", "repo_map", "memory", "relevant_memory", "history", "current_request"
    ]


def test_priority_reduction_is_recorded_in_tokens(tmp_path):
    agent = build_agent(tmp_path)
    agent.prefix = "rules " + "A " * 800
    agent.memory.render_panel = lambda: "memory " + "B " * 400
    manager = ContextManager(
        agent,
        total_budget=500,
        section_budgets={"prefix": 400, "memory": 180, "relevant_memory": 100, "history": 180},
        section_floors={"prefix": 100, "memory": 40, "relevant_memory": 20, "history": 50},
    )

    _, metadata = manager.build("keep this request")

    assert metadata["budget_reductions"]
    assert metadata["budget_allocation"]["strategy"] == "floor_weighted_shared_pool"
    reduced_sections = [item["section"] for item in metadata["budget_reductions"]]
    assert reduced_sections == [section for section in manager.reduction_order if section in reduced_sections]
    assert all("before_tokens" in item for item in metadata["budget_reductions"])


def test_request_larger_than_runtime_budget_is_rejected(tmp_path):
    agent = build_agent(tmp_path, max_new_tokens=100)
    with pytest.raises(ContextBudgetExceeded):
        ContextManager(agent, total_budget=120).build("X " * 100)


def test_ledger_compaction_keeps_audit_entries_and_changes_active_projection(tmp_path):
    agent = build_agent(tmp_path)
    state = TaskState.create("task_test", "inspect", run_id="run_test")
    agent.current_task_state = state
    agent.run_store.start_run(state)
    ledger = ContextLedger(state.run_id, agent.run_store)
    ledger.append_user("inspect")
    for index in range(5):
        call = ToolCall("read_file", {"path": "README.md", "start": 1, "end": 1}, f"call_{index}")
        ledger.append_tool_call(call)
        ledger.append_tool_result(
            ToolOutcome(
                call.call_id, call.name, "ok", "completed", "none", "result " + "x " * 100,
                f"fp_{index}", {"status": "admitted", "stages": []}
            )
        )
    agent.context_ledger = ledger

    _, metadata = ContextManager(
        agent,
        total_budget=800,
        section_budgets={"prefix": 300, "memory": 100, "relevant_memory": 80, "history": 120},
    ).build("continue")

    assert ledger.generation == 2
    assert any(entry.kind == "compaction_summary" for entry in ledger.entries)
    assert len(ledger.active_entries()) < len(ledger.entries)
    assert metadata["ledger_generation"] == 2


def test_current_run_session_events_are_not_duplicated_with_ledger(tmp_path):
    agent = build_agent(tmp_path)
    state = TaskState.create("task_dedupe", "inspect", run_id="run_dedupe")
    agent.current_task_state = state
    agent.run_store.start_run(state)
    ledger = ContextLedger(state.run_id, agent.run_store)
    ledger.append_user("inspect")
    call = ToolCall("read_file", {"path": "README.md", "start": 1, "end": 1}, "call_dedupe")
    ledger.append_tool_call(call)
    ledger.append_tool_result(
        ToolOutcome(
            call.call_id,
            call.name,
            "ok",
            "completed",
            "none",
            "unique-current-run-result",
            "fp_dedupe",
            {"status": "admitted", "stages": []},
        )
    )
    agent.context_ledger = ledger
    agent.record({"role": "user", "content": "inspect"})
    agent.record({"role": "tool", "name": "read_file", "args": call.args, "content": "unique-current-run-result"})

    prompt, metadata = ContextManager(agent, total_budget=900).build("inspect")

    assert prompt.count("unique-current-run-result") == 1
    assert prompt.count("Current user request:\ninspect") == 1
    assert metadata["history_projection"]["current_run_duplicates_avoided"] == 2
    assert metadata["history_projection"]["ledger"]["current_request_duplicate_avoided"] == 1
    assert metadata["history_projection"]["source"] == "ledger_plus_prior_runs"


def test_shared_budget_lends_unused_tokens_to_history(tmp_path):
    agent = build_agent(tmp_path)
    agent.prefix = "short rules"
    agent.memory.render_panel = lambda: "Memory:\n- short"
    for index in range(8):
        agent.record({"role": "assistant", "content": f"history-{index} " + "long-context " * 80})
    manager = ContextManager(
        agent,
        total_budget=600,
        section_budgets={"prefix": 80, "memory": 80, "relevant_memory": 80, "history": 80},
        section_floors={"prefix": 20, "memory": 20, "relevant_memory": 20, "history": 20},
    )

    _, metadata = manager.build("continue")

    allocation = metadata["budget_allocation"]
    assert allocation["strategy"] == "floor_weighted_shared_pool"
    assert allocation["allocated_tokens"]["history"] > 80
    assert allocation["borrowed_tokens"]["history"] > 0


def test_bounded_tool_result_uses_executor_projection_without_second_truncation(tmp_path):
    agent = build_agent(tmp_path)
    state = TaskState.create("task_bounded", "inspect", run_id="run_bounded")
    agent.run_store.start_run(state)
    ledger = ContextLedger(state.run_id, agent.run_store)
    ledger.append_user("inspect")
    call = ToolCall("read_file", {"path": "large.log"}, "call_bounded")
    ledger.append_tool_call(call)
    bounded = (
        "head\n" + "x" * 3900 + "\ntail\n"
        "[Output truncated; use read_artifact artifact_id=tool_call_bounded_deadbeef]"
    )
    entry = ledger.append_tool_result(
        ToolOutcome(
            call.call_id,
            call.name,
            "ok",
            "completed",
            "none",
            bounded,
            "fp_bounded",
            {"status": "admitted", "stages": []},
            artifact_id="tool_call_bounded_deadbeef",
            artifact={"size_bytes": 12000},
            metadata={"output_truncated": True},
        )
    )

    assert entry.content_tier == "artifact_reference"
    assert entry.content == bounded
    assert entry.original_size_bytes == 12000
    assert entry.artifact_id == "tool_call_bounded_deadbeef"


def test_structured_compaction_preserves_key_facts_and_repeated_summary(tmp_path):
    agent = build_agent(tmp_path)
    state = TaskState.create("task_summary", "inspect deploy", run_id="run_summary")
    agent.run_store.start_run(state)
    ledger = ContextLedger(state.run_id, agent.run_store)
    ledger.append_user("inspect deploy")
    call = ToolCall("read_file", {"path": "deploy.txt"}, "call_summary")
    ledger.append_tool_call(call)
    ledger.append_tool_result(
        ToolOutcome(
            call.call_id,
            call.name,
            "ok",
            "completed",
            "none",
            "deploy target is staging",
            "fp_summary",
            {"status": "admitted", "stages": []},
        )
    )
    source = ledger.active_entries()
    first = ledger.commit_compaction(
        ledger.build_structured_summary(source),
        [entry.entry_id for entry in source],
        expected_generation=ledger.generation,
        expected_active_digest=ledger.active_digest(),
    )
    ledger.append_guidance("verify staging before release")
    merged = ledger.build_structured_summary(ledger.active_entries())

    assert first.summary["key_facts"] == ["read_file(deploy.txt): deploy target is staging"]
    assert merged["key_facts"] == first.summary["key_facts"]
    assert merged["next_steps"] == ["verify staging before release"]


def test_compaction_rejects_stale_transaction_and_split_batch(tmp_path):
    agent = build_agent(tmp_path)
    state = TaskState.create("task_tx", "inspect", run_id="run_tx")
    agent.run_store.start_run(state)
    ledger = ContextLedger(state.run_id, agent.run_store)
    ledger.append_user("inspect")
    digest = ledger.active_digest()
    generation = ledger.generation
    ledger.append_guidance("new state")

    with pytest.raises(RuntimeError, match="changed"):
        ledger.commit_compaction(
            {"summary": ["old"]},
            ["run_tx:ctx:000001"],
            expected_generation=generation,
            expected_active_digest=digest,
        )

    call = ToolCall("read_file", {"path": "README.md"}, "call_tx")
    ledger.append_tool_call(call)
    ledger.append_tool_result(
        ToolOutcome(call.call_id, call.name, "ok", "completed", "none", "ok", "fp", {"status": "admitted", "stages": []})
    )
    with pytest.raises(ValueError, match="split"):
        ledger.commit_compaction(
            {"summary": ["bad"]},
            [entry.entry_id for entry in ledger.active_entries()[:-1]],
            expected_generation=ledger.generation,
            expected_active_digest=ledger.active_digest(),
        )


def test_restore_reconciles_interrupted_operation_without_replay(tmp_path):
    agent = build_agent(tmp_path)
    state = TaskState.create("task_crash", "edit", run_id="run_crash")
    agent.run_store.start_run(state)
    ledger = ContextLedger(state.run_id, agent.run_store)
    ledger.append_user("edit")
    call = ToolCall("patch_file", {"path": "README.md"}, "call_crash")
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
    result = restored.entries[-1]
    assert result.kind == "tool_result"
    assert result.side_effect_state == "unknown"
    assert "do not replay" in result.content
