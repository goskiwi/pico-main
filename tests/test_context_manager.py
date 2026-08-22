import pytest

from pico import FakeModelClient, Pico, PicoConfig, SessionStore, WorkspaceContext
from pico.context_manager import ContextBudgetExceeded, ContextManager
from pico.contracts import ToolCall, ToolOutcome
from pico.features.memory import WorkingState
from pico.run_log import RunLog, replay_events
from pico.task_state import TaskState


def build_agent(tmp_path, max_new_tokens=64):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        config=PicoConfig(
            approval_policy="auto",
            max_new_tokens=max_new_tokens,
        ),
    )


def new_run_log(agent, state):
    return RunLog(
        state.run_id,
        state.task_id,
        agent.session.data["id"],
        agent.dependencies.run_store,
    )


def set_working_state(agent, goal, **overrides):
    state = TaskState.create("task_working", goal, run_id="run_working")
    state.working_state = WorkingState(goal=goal, **overrides)
    agent.run.task_state = state
    return state


def successful_outcome(call, content, *, artifact=None):
    return ToolOutcome(
        tool_call_id=call.call_id,
        tool_name=call.name,
        status="success",
        execution_state="completed",
        side_effect_state="none",
        content=content,
        artifact=dict(artifact or {}),
    )


def append_successful_result(run_log, call, outcome):
    run_log.append_tool_started(
        call,
        risky=False,
        effect_scope="none",
        potential_effects=[],
    )
    return run_log.append_tool_result(outcome, workspace_revision=0)


def test_context_uses_token_budgets_and_preserves_request(tmp_path):
    agent = build_agent(tmp_path)
    set_working_state(agent, "deploy key is red")
    prompt, metadata = ContextManager(agent, total_budget=900).build("Where is the deploy key?")

    assert prompt.rstrip().endswith("Current user request:\nWhere is the deploy key?")
    assert metadata["within_budget"] is True
    assert metadata["prompt_tokens"] + metadata["reserved_output_tokens"] <= 900
    assert metadata["tokenizer"]
    assert metadata["section_order"] == [
        "prefix",
        "memory_catalog",
        "repo_map",
        "working_state",
        "history",
        "current_request",
    ]


def test_context_budget_reserves_known_provider_request_overhead(tmp_path):
    class ToolAwareClient(FakeModelClient):
        @staticmethod
        def estimate_action_tool_tokens(_action_tools, _token_counter):
            return 120

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = Pico(
        ToolAwareClient([]),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico" / "sessions"),
        config=PicoConfig(max_new_tokens=64),
    )

    _prompt, metadata = ContextManager(agent, total_budget=1200).build(
        "Inspect the repository",
        provider_overhead_tokens=80,
    )

    assert metadata["tool_schema_tokens"] == 120
    assert metadata["provider_overhead_tokens"] == 80
    assert metadata["estimated_input_tokens"] == metadata["prompt_tokens"] + 200
    assert metadata["estimated_input_tokens"] + 64 <= 1200
    assert metadata["within_budget"] is True


def test_priority_reduction_is_recorded_in_tokens(tmp_path):
    agent = build_agent(tmp_path)
    agent.prompt.prefix = "rules " + "A " * 800
    state = set_working_state(agent, "large memory")
    state.working_state.render_panel = lambda **_kwargs: "memory " + "B " * 400
    manager = ContextManager(
        agent,
        total_budget=500,
        section_budgets={
            "prefix": 400,
            "memory_catalog": 80,
            "working_state": 180,
            "history": 180,
        },
        section_floors={
            "prefix": 100,
            "memory_catalog": 20,
            "working_state": 40,
            "history": 50,
        },
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


def test_runtime_context_uses_the_provider_window(tmp_path):
    agent = build_agent(tmp_path)

    assert agent.prompt.context.total_budget == 272_000


def test_default_compaction_threshold_uses_exact_reserve(tmp_path):
    agent = build_agent(tmp_path)
    state = TaskState.create("task_default_threshold", "inspect", run_id="run_default_threshold")
    agent.run.task_state = state
    run_log = new_run_log(agent, state)
    run_log.append_user("inspect")
    for index in range(12):
        call = ToolCall("read_file", {"path": f"file_{index}.py"}, f"call_default_{index}")
        run_log.append_tool_call(call)
        append_successful_result(
            run_log, call, successful_outcome(call, "result " + "x " * 3000)
        )
    agent.run.run_log = run_log

    _, at_threshold = agent.prompt.context.build(
        "continue",
        provider_context_tokens=255_616,
    )
    _, above_threshold = agent.prompt.context.build(
        "continue",
        provider_context_tokens=255_617,
    )

    assert at_threshold["compaction"] is None
    assert above_threshold["compaction"]["trigger_threshold_tokens"] == 255_616
    assert above_threshold["compaction"]["trigger_context_tokens"] == 255_617
    assert above_threshold["compaction"]["retained_tokens"] <= 20_000


def test_small_run_log_is_not_compacted_by_event_count(tmp_path):
    agent = build_agent(tmp_path)
    state = TaskState.create("task_small", "inspect", run_id="run_small")
    run_log = new_run_log(agent, state)
    run_log.append_user("inspect")
    for index in range(12):
        run_log.append_model_instruction(f"small note {index}")
    agent.run.run_log = run_log
    summary_requested = False

    def summarize(*args, **kwargs):
        nonlocal summary_requested
        summary_requested = True
        return "must not run"

    agent.model_client.complete = summarize
    _, metadata = ContextManager(
        agent,
        total_budget=5000,
        compaction_reserve_tokens=500,
        compaction_keep_recent_tokens=1000,
    ).build("continue")

    assert len(run_log.active_events()) == 13
    assert metadata["compaction"] is None
    assert summary_requested is False


def test_provider_usage_can_trigger_compaction_when_local_prompt_is_under_limit(
    tmp_path,
):
    agent = build_agent(tmp_path)
    state = TaskState.create("task_provider_usage", "inspect", run_id="run_provider_usage")
    agent.run.task_state = state
    run_log = new_run_log(agent, state)
    run_log.append_user("inspect")
    for index in range(5):
        call = ToolCall("read_file", {"path": f"file_{index}.py"}, f"call_{index}")
        run_log.append_tool_call(call)
        append_successful_result(
            run_log, call, successful_outcome(call, "result " + "x " * 500)
        )
    agent.run.run_log = run_log

    _, metadata = ContextManager(
        agent,
        total_budget=10_000,
        compaction_reserve_tokens=2_000,
        compaction_keep_recent_tokens=100,
    ).build("continue", provider_context_tokens=9_500)

    assert metadata["compaction"] is not None
    assert metadata["compaction"]["trigger_context_tokens"] == 9_500
    assert metadata["compaction"]["local_context_tokens"] < 8_000


def test_runtime_preserves_context_beyond_the_old_eight_thousand_token_cap(tmp_path):
    agent = build_agent(tmp_path)
    state = TaskState.create("task_large", "inspect", run_id="run_large")
    run_log = new_run_log(agent, state)
    run_log.append_user("inspect")
    run_log.append_model_instruction("context " * 9000 + "END-OF-LARGE-CONTEXT")
    agent.run.run_log = run_log

    prompt, metadata = agent.prompt.context.build("continue")

    assert metadata["prompt_tokens"] > 8000
    assert metadata["compaction"] is None
    assert "END-OF-LARGE-CONTEXT" in prompt


def test_run_log_compaction_keeps_audit_entries_and_changes_active_projection(tmp_path):
    agent = build_agent(tmp_path)
    state = TaskState.create("task_test", "inspect", run_id="run_test")
    agent.run.task_state = state
    run_log = new_run_log(agent, state)
    run_log.append_user("inspect")
    for index in range(5):
        call = ToolCall("read_file", {"path": "README.md", "start": 1, "end": 1}, f"call_{index}")
        run_log.append_tool_call(call)
        append_successful_result(
            run_log, call, successful_outcome(call, "result " + "x " * 100)
        )
    agent.run.run_log = run_log

    _, metadata = ContextManager(
        agent,
        total_budget=800,
        section_budgets={
            "prefix": 300,
            "memory_catalog": 60,
            "working_state": 100,
            "history": 120,
        },
        compaction_reserve_tokens=200,
        compaction_keep_recent_tokens=100,
    ).build("continue")

    summary = next(entry for entry in run_log.events if entry.kind == "compaction")
    assert run_log.generation == 2
    assert summary.content.startswith("Earlier run summary:")
    assert (
        '- Tool transaction: read_file {"end": 1, "path": "README.md", '
        '"start": 1} -> success'
    ) in summary.content
    assert "- Goal:" not in summary.content
    assert len(run_log.active_events()) < len(run_log.events)
    assert metadata["run_log_generation"] == 2
    assert metadata["compaction"]["mode"] == "runtime_summary"
    projection = replay_events(run_log.events)
    assert projection.last_cursor.sequence == summary.sequence
    assert projection.kind_counts["compaction"] == 1


def test_repeated_compaction_keeps_completed_tool_arguments(tmp_path):
    agent = build_agent(tmp_path)
    state = TaskState.create("task_steps", "read files", run_id="run_steps")
    agent.run.task_state = state
    run_log = new_run_log(agent, state)
    run_log.append_user("read files")
    agent.run.run_log = run_log
    manager = ContextManager(
        agent,
        total_budget=10_000,
        compaction_reserve_tokens=2_000,
        compaction_keep_recent_tokens=100,
    )

    paths = [f"noise_{label}.txt" for label in "abcde"]
    for index, path in enumerate(paths):
        call = ToolCall(
            "read_file",
            {"path": path, "start": 1, "end": 700},
            f"call_step_{index}",
        )
        run_log.append_tool_call(call)
        append_successful_result(
            run_log,
            call,
            successful_outcome(call, "irrelevant payload " + "x " * 1000),
        )
        manager.build("continue", provider_context_tokens=9_500)

    history = manager._history_text("continue")

    assert run_log.generation > 2
    assert all(path in history for path in paths)


def test_current_run_session_events_are_not_duplicated_with_run_log(tmp_path):
    agent = build_agent(tmp_path)
    state = TaskState.create("task_dedupe", "inspect", run_id="run_dedupe")
    agent.run.task_state = state
    run_log = new_run_log(agent, state)
    run_log.append_user("inspect")
    call = ToolCall("read_file", {"path": "README.md", "start": 1, "end": 1}, "call_dedupe")
    run_log.append_tool_call(call)
    append_successful_result(
        run_log, call, successful_outcome(call, "unique-current-run-result")
    )
    agent.run.run_log = run_log

    prompt, metadata = ContextManager(agent, total_budget=900).build("inspect")

    assert prompt.count("unique-current-run-result") == 1
    assert prompt.count("Current user request:\ninspect") == 1
    assert metadata["history_projection"]["active_count"] == 3
    assert metadata["history_projection"]["selected_count"] == 2


def test_working_state_projection_replaces_successful_update_transcript(tmp_path):
    agent = build_agent(tmp_path)
    state = TaskState.create("task_working", "Fix login timeout", run_id="run_working")
    agent.run.task_state = state
    run_log = new_run_log(agent, state)
    run_log.append_user(state.working_state.goal)
    agent.run.run_log = run_log
    update = {
        "add_constraints": ["Do not change the database schema"],
        "add_decisions": ["The race is in token refresh"],
        "add_next_steps": ["Add a concurrent refresh test"],
    }
    call = ToolCall("update_working_state", update, "call_working")
    agent.apply_run_event(run_log.append_tool_call(call))
    assert agent.tools.run(call).status == "success"

    prompt, _metadata = ContextManager(agent, total_budget=1200).build(
        "Fix login timeout"
    )
    history = agent.prompt.context._history_text("Fix login timeout")

    assert prompt.count("Fix login timeout") == 1
    assert prompt.count("Do not change the database schema") == 1
    assert prompt.count("The race is in token refresh") == 1
    assert prompt.count("Add a concurrent refresh test") == 1
    assert "update_working_state" not in history
    assert "working state update accepted" not in history


def test_rejected_working_state_update_remains_in_history(tmp_path):
    agent = build_agent(tmp_path)
    state = TaskState.create("task_working", "Inspect", run_id="run_working")
    agent.run.task_state = state
    run_log = new_run_log(agent, state)
    run_log.append_user(state.working_state.goal)
    agent.run.run_log = run_log
    call = ToolCall(
        "update_working_state",
        {
            "add_constraints": ["Keep the API"],
            "remove_constraints": ["Keep the API"],
        },
        "call_rejected_working",
    )
    agent.apply_run_event(run_log.append_tool_call(call))
    assert agent.tools.run(call).status == "rejected"

    history = agent.prompt.context._history_text("Inspect")

    assert "update_working_state" in history
    assert "cannot add and remove" in history


def test_shared_budget_lends_unused_tokens_to_history(tmp_path):
    agent = build_agent(tmp_path)
    agent.prompt.prefix = "short rules"
    state = set_working_state(agent, "short")
    state.working_state.render_panel = lambda **_kwargs: "Memory:\n- short"
    run_log = new_run_log(agent, state)
    run_log.append_user(state.working_state.goal)
    run_log.append_final("long-context " * 160)
    agent.run.run_log = run_log
    manager = ContextManager(
        agent,
        total_budget=600,
        section_budgets={
            "prefix": 80,
            "memory_catalog": 80,
            "working_state": 80,
            "history": 80,
        },
        section_floors={
            "prefix": 20,
            "memory_catalog": 20,
            "working_state": 20,
            "history": 20,
        },
    )

    _, metadata = manager.build("continue")

    allocation = metadata["budget_allocation"]
    assert allocation["strategy"] == "floor_weighted_shared_pool"
    assert allocation["allocated_tokens"]["history"] > 80
    assert allocation["borrowed_tokens"]["history"] > 0


def test_memory_catalog_does_not_automatically_load_card_bodies(tmp_path):
    agent = build_agent(tmp_path)
    set_working_state(agent, "unique-working-goal")
    agent.dependencies.project_memory.store(
        action="create",
        filename="project_selected.md",
        name="Selected convention",
        description="A stable convention that may be recalled explicitly.",
        memory_type="project",
        content="PRIVATE-CARD-BODY",
        why="It is stable across tasks.",
        how_to_apply="Recall it only for a matching task.",
        source_run_id="bootstrap",
    )
    manager = ContextManager(
        agent,
        total_budget=900,
        section_budgets={
            "prefix": 120,
            "memory_catalog": 40,
            "repo_map": 80,
            "working_state": 80,
            "history": 120,
        },
        section_floors={
            "prefix": 40,
            "memory_catalog": 10,
            "repo_map": 20,
            "working_state": 20,
            "history": 30,
        },
    )

    prompt, metadata = manager.build("use selected memory")

    assert "project_selected.md" in prompt
    assert "PRIVATE-CARD-BODY" not in prompt
    assert prompt.count("unique-working-goal") == 1
    assert "retrieved_memory" not in metadata


def test_bounded_tool_result_uses_executor_projection_without_second_truncation(tmp_path):
    agent = build_agent(tmp_path)
    state = TaskState.create("task_bounded", "inspect", run_id="run_bounded")
    run_log = new_run_log(agent, state)
    run_log.append_user("inspect")
    call = ToolCall("read_file", {"path": "large.log"}, "call_bounded")
    run_log.append_tool_call(call)
    bounded = (
        "head\n" + "x" * 3900 + "\ntail\n"
        "[Output truncated; use read_artifact artifact_id=tool_call_bounded_deadbeef]"
    )
    entry = append_successful_result(
        run_log,
        call,
        successful_outcome(
            call,
            bounded,
            artifact={
                "artifact_id": "tool_call_bounded_deadbeef",
                "size_bytes": 12000,
            },
        )
    )

    assert entry.content == bounded
    assert entry.artifact_id == "tool_call_bounded_deadbeef"
    assert entry.payload["outcome"]["artifact"]["size_bytes"] == 12000


def test_pending_tool_call_prevents_compaction_and_summary_request(tmp_path):
    agent = build_agent(tmp_path)
    state = TaskState.create("task_pending", "continue", run_id="run_pending")
    agent.run.task_state = state
    run_log = new_run_log(agent, state)
    run_log.append_user("inspect")
    for index in range(5):
        call = ToolCall(
            "read_file",
            {"path": f"file_{index}.py"},
            f"call_complete_{index}",
        )
        run_log.append_tool_call(call)
        append_successful_result(
            run_log, call, successful_outcome(call, "x " * 500)
        )
    run_log.append_tool_call(
        ToolCall("read_file", {"path": "pending.py"}, "call_pending")
    )
    agent.run.run_log = run_log
    summary_requested = False

    def summarize(*args, **kwargs):
        nonlocal summary_requested
        summary_requested = True
        return "must not run"

    agent.model_client.complete = summarize
    _, metadata = ContextManager(agent, total_budget=3000).build("continue")

    assert run_log.generation == 1
    assert run_log.pending_call_id() == "call_pending"
    assert metadata["compaction"] is None
    assert summary_requested is False


def test_restore_reconciles_interrupted_operation_without_replay(tmp_path):
    agent = build_agent(tmp_path)
    state = TaskState.create("task_crash", "edit", run_id="run_crash")
    run_log = new_run_log(agent, state)
    run_log.append_user("edit")
    call = ToolCall("patch_file", {"path": "README.md"}, "call_crash")
    run_log.append_tool_call(call)
    run_log.append_tool_started(
        call,
        risky=True,
        effect_scope="workspace",
        potential_effects=[],
    )
    restored = RunLog.restore(state.run_id, agent.dependencies.run_store)
    agent.run.task_state = state
    agent.run.run_log = restored
    restored.reconcile_interrupted(agent)

    assert restored.pending_call_id() == ""
    result = restored.events[-1]
    assert result.kind == "tool_result"
    assert result.side_effect_state == "unknown"
    assert "interrupted" in result.content
