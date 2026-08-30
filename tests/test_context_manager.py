import pytest

from pico import FakeModelClient, Pico, PicoConfig, SessionStore, WorkspaceContext
from pico.compaction_summary import SemanticCompactionError
from pico.context_manager import ContextBudgetExceeded, ContextManager
from pico.contracts import FailureInfo, ToolCall, ToolOutcome
from pico.run_log import RunLog
from pico.run_projection import RunProjection
from pico.task_state import TaskContract

READ_TASK = {
    "task_kind": "read_only",
    "requires_workspace_change": False,
    "requires_verification": False,
}


def build_agent(tmp_path, max_new_tokens=64):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pico(
        FakeModelClient([]),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico" / "sessions"),
        config=PicoConfig(approval_policy="auto", max_new_tokens=max_new_tokens),
    )


def activate(agent, goal="Inspect"):
    contract = TaskContract(goal=goal, **READ_TASK)
    run_log = RunLog(
        "run_context",
        "task_context",
        agent.session.data["id"],
        agent.dependencies.run_store,
    )
    run_log.append_user(contract)
    agent.run.run_log = run_log
    agent.run.projection = RunProjection.from_events(run_log.events)
    return run_log


def append_read(run_log, index, content):
    call = ToolCall("read_file", {"path": f"file_{index}.py"}, f"call_{index}")
    run_log.append_tool_call(call)
    run_log.append_tool_started(
        call,
        risky=False,
        effect_scope="none",
        potential_effects=[],
    )
    outcome = ToolOutcome(
        tool_call_id=call.call_id,
        tool_name=call.name,
        status="success",
        execution_state="completed",
        side_effect_state="none",
        content=content,
    )
    run_log.append_tool_result(outcome)


def test_context_separates_dynamic_input_and_preserves_request(tmp_path):
    agent = build_agent(tmp_path)
    activate(agent, "Inspect README")

    input_text, metadata = ContextManager(agent, total_budget=1800).build(
        "Inspect README"
    )

    assert input_text.rstrip().endswith("Current user request:\nInspect README")
    assert "Task contract (Runtime-owned):" in input_text
    assert "- goal: Inspect README" in input_text
    assert "Runtime rules:" not in input_text
    assert metadata["instructions_tokens"] > 0
    assert metadata["section_order"] == [
        "workspace",
        "task_requirements",
        "memory_catalog",
        "repo_map",
        "working_state",
        "history",
        "current_request",
    ]


def test_fixed_caps_leave_the_remaining_budget_to_history(tmp_path):
    agent = build_agent(tmp_path)
    run_log = activate(agent)
    run_log.append_model_instruction("history " * 500)
    manager = ContextManager(
        agent,
        total_budget=1200,
        section_caps={
            "workspace": 100,
            "task_requirements": 80,
            "memory_catalog": 40,
            "repo_map": 40,
            "working_state": 80,
        },
    )

    _, metadata = manager.build("continue")

    allocation = metadata["budget_allocation"]
    assert allocation["strategy"] == "fixed_caps_history_remainder"
    assert allocation["history_budget_tokens"] == metadata["sections"]["history"][
        "budget_tokens"
    ]
    assert "borrowed_tokens" not in allocation


def test_prompt_build_is_read_only_even_above_compaction_threshold(tmp_path):
    agent = build_agent(tmp_path)
    run_log = activate(agent)
    for index in range(5):
        append_read(run_log, index, "result " + "x " * 400)
    before = tuple(run_log.events)
    generation = run_log.generation

    _, metadata = ContextManager(
        agent,
        total_budget=1200,
        compaction_reserve_tokens=200,
        compaction_keep_recent_tokens=100,
    ).build("continue", provider_context_tokens=1100)

    assert tuple(run_log.events) == before
    assert run_log.generation == generation
    assert metadata["compaction"] is None


def test_prepare_compaction_commits_before_read_only_build(tmp_path):
    class Summary:
        def __init__(self):
            self.calls = []

        def summarize(self, events, **_kwargs):
            self.calls.append(
                {"duration_ms": 1, "completion_metadata": {"input_tokens": 10}}
            )
            return (
                "## Progress\n### Done\n- SEMANTIC-SUMMARY-MARKER\n\n"
                "## Critical Context\n- none"
            )

    agent = build_agent(tmp_path)
    run_log = activate(agent)
    for index in range(5):
        append_read(run_log, index, "result " + "x " * 300)
    manager = ContextManager(
        agent,
        total_budget=900,
        compaction_reserve_tokens=200,
        compaction_keep_recent_tokens=100,
    )
    manager.semantic_summarizer = Summary()

    compaction, history_override = manager.prepare_compaction("continue")
    event_count = len(run_log.events)
    input_text, metadata = manager.build(
        "continue",
        compaction_metadata=compaction,
        history_override=history_override,
    )

    assert compaction["mode"] == "semantic_history"
    assert compaction["committed"] is True
    assert history_override is None
    assert len(run_log.events) == event_count
    assert any(
        event.kind == "compaction"
        and "SEMANTIC-SUMMARY-MARKER" in event.content
        for event in run_log.events
    )
    assert "SEMANTIC-SUMMARY-MARKER" in input_text
    assert metadata["sections"]["history"]["raw_tokens"] == metadata["sections"][
        "history"
    ]["rendered_tokens"]
    assert (
        metadata["history_projection"]["projection_mode"]
        == "compacted_complete_transactions"
    )
    assert input_text.endswith("Current user request:\ncontinue")
    assert metadata["compaction"] == compaction


def test_semantic_summary_must_fit_with_the_omitted_hint_before_commit(tmp_path):
    agent = build_agent(tmp_path)
    run_log = activate(agent)
    for index in range(5):
        append_read(run_log, index, "result " + "x " * 300)
    manager = ContextManager(
        agent,
        total_budget=900,
        compaction_reserve_tokens=200,
        compaction_keep_recent_tokens=100,
    )
    raw = manager._raw_sections("continue")
    overhead = manager.tokenizer.count(agent.prompt.instructions)
    overhead += manager._tool_schema_tokens()
    history_budget = manager._history_budget(raw, overhead)
    prefix = "Current run events:\n[compaction] "
    marker = " UNIQUE-END-MARKER"
    candidates = []
    for size in range(1000):
        summary = "## Progress\n### Done\n- " + "x " * size + marker
        tokens = manager.tokenizer.count(prefix + summary)
        if tokens <= history_budget:
            candidates.append((tokens, summary))
        elif candidates:
            break
    _tokens, boundary_summary = max(candidates)

    class Summary:
        def __init__(self):
            self.calls = []

        def summarize(self, _events, **_kwargs):
            self.calls.append({"duration_ms": 1, "completion_metadata": {}})
            return boundary_summary

    manager.semantic_summarizer = Summary()
    before = tuple(run_log.events)

    metadata, history = manager.prepare_compaction("continue")

    assert metadata["degraded"] is True
    assert metadata["committed"] is False
    assert tuple(run_log.events) == before
    assert "bounded fallback" in history


def test_semantic_failure_uses_complete_transaction_fallback_without_event(tmp_path):
    class FailingSummary:
        def __init__(self):
            self.calls = []

        def summarize(self, *_args, **_kwargs):
            raise SemanticCompactionError("planned failure")

    agent = build_agent(tmp_path)
    run_log = activate(agent)
    for index in range(5):
        append_read(run_log, index, "result " + "x " * 300)
    manager = ContextManager(
        agent,
        total_budget=900,
        compaction_reserve_tokens=200,
        compaction_keep_recent_tokens=180,
    )
    manager.semantic_summarizer = FailingSummary()
    before = tuple(run_log.events)

    metadata, history = manager.prepare_compaction("continue")

    assert tuple(run_log.events) == before
    assert metadata["degraded"] is True
    assert metadata["committed"] is False
    assert "bounded fallback" in history
    for call_id in {
        event.call_id
        for event in before
        if event.kind == "assistant_tool_call"
        and event.call_id in history
    }:
        assert history.count(call_id) == 2


def test_pending_tool_call_skips_compaction(tmp_path):
    agent = build_agent(tmp_path)
    run_log = activate(agent)
    run_log.append_tool_call(
        ToolCall("read_file", {"path": "pending.py"}, "call_pending")
    )
    manager = ContextManager(agent, total_budget=300)

    assert manager.prepare_compaction("continue", provider_context_tokens=299) == (
        None,
        None,
    )


def test_memory_catalog_does_not_load_card_body(tmp_path):
    agent = build_agent(tmp_path)
    activate(agent, "Use selected memory")
    agent.dependencies.project_memory.store(
        action="create",
        filename="project_selected.md",
        name="Selected convention",
        description="A stable convention.",
        memory_type="project",
        content="PRIVATE-CARD-BODY",
        why="Stable across tasks.",
        how_to_apply="Recall only when relevant.",
        source_run_id="bootstrap",
    )

    input_text, _ = ContextManager(agent, total_budget=1800).build(
        "Use selected memory"
    )

    assert "project_selected.md" in input_text
    assert "PRIVATE-CARD-BODY" not in input_text


def test_request_larger_than_runtime_budget_is_rejected(tmp_path):
    agent = build_agent(tmp_path, max_new_tokens=100)
    with pytest.raises(ContextBudgetExceeded):
        ContextManager(agent, total_budget=120).build("X " * 100)


def test_provider_overhead_is_reserved_in_the_input_budget(tmp_path):
    agent = build_agent(tmp_path)
    activate(agent)
    _input, metadata = ContextManager(agent, total_budget=1800).build(
        "Inspect",
        provider_overhead_tokens=137,
    )

    assert metadata["provider_overhead_tokens"] == 137
    assert metadata["estimated_input_tokens"] + metadata["reserved_output_tokens"] <= 1800


def test_context_uses_the_configured_provider_window(tmp_path):
    agent = build_agent(tmp_path)

    assert agent.prompt.context.total_budget == 272_000


def test_provider_usage_can_trigger_explicit_compaction(tmp_path):
    class Summary:
        def __init__(self):
            self.calls = []
            self.seen_events = []

        def summarize(self, events, **_kwargs):
            self.seen_events.append(tuple(events))
            self.calls.append({"duration_ms": 1, "completion_metadata": {}})
            return "## Progress\n### Done\n- inspected\n\n## Critical Context\n- none"

    agent = build_agent(tmp_path)
    run_log = activate(agent)
    for index in range(5):
        append_read(run_log, index, "result " + "x " * 500)
    manager = ContextManager(
        agent,
        total_budget=10_000,
        compaction_reserve_tokens=2_000,
        compaction_keep_recent_tokens=100,
    )
    summary = Summary()
    manager.semantic_summarizer = summary

    metadata, history_override = manager.prepare_compaction(
        "continue",
        provider_context_tokens=9_500,
    )

    assert metadata["trigger_context_tokens"] == 9_500
    assert metadata["committed"] is True
    assert history_override is None
    assert summary.seen_events


def test_large_history_is_not_clipped_to_a_legacy_eight_thousand_limit(tmp_path):
    agent = build_agent(tmp_path)
    run_log = activate(agent)
    run_log.append_model_instruction("context " * 9000 + "END-OF-LARGE-CONTEXT")

    input_text, metadata = agent.prompt.context.build("continue")

    assert metadata["input_text_tokens"] > 8_000
    assert "END-OF-LARGE-CONTEXT" in input_text


def test_history_omits_canonical_contract_and_successful_working_update(tmp_path):
    agent = build_agent(tmp_path)
    run_log = activate(agent, "Canonical goal")
    call = ToolCall(
        "update_working_state",
        {"add_constraints": ["Keep API"]},
        "call_state",
    )
    run_log.append_tool_call(call)
    run_log.append_tool_started(
        call,
        risky=False,
        effect_scope="none",
        potential_effects=[],
    )
    run_log.append_tool_result(
        ToolOutcome(
            call.call_id,
            call.name,
            "success",
            "completed",
            "none",
            "accepted",
        )
    )

    history, metadata = run_log.render_projection()

    assert "Canonical goal" not in history
    assert "update_working_state" not in history
    assert metadata["omitted_count"] == 3


def test_rejected_working_update_remains_visible_in_history(tmp_path):
    agent = build_agent(tmp_path)
    run_log = activate(agent)
    call = ToolCall(
        "update_working_state",
        {"add_constraints": ["Keep API"]},
        "call_rejected_state",
    )
    run_log.append_tool_call(call)
    run_log.append_tool_result(
        ToolOutcome(
            call.call_id,
            call.name,
            "rejected",
            "not_started",
            "none",
            "rejected",
            failure=FailureInfo("invalid_arguments", "planned", "retry_after_change"),
        )
    )

    history, _metadata = run_log.render_projection()

    assert "update_working_state" in history
    assert "invalid_arguments" in history
