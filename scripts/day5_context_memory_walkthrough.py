"""Day 5: separate default context enhancement from Context Pressure."""

import json
import tempfile
from pathlib import Path

from pico import (
    FakeModelClient,
    Pico,
    PicoConfig,
    SessionStore,
    ToolCall,
    ToolOutcome,
    WorkspaceContext,
)
from pico.execution import ExecutionContext
from pico.run_log import RunLog
from pico.run_projection import RunProjection
from pico.task_state import TaskContract

QUERY = "Where is calculate_invoice_total used?"
MEMORY_FILENAME = "reference_invoice_checks.md"
MEMORY_LITERAL = "RUN_ONLY_THE_TARGETED_INVOICE_TEST_FIRST"
SUMMARY_MARKER = "SEMANTIC-SUMMARY-MARKER"


def print_section(title, value):
    print(f"\n=== {title} ===")
    print(json.dumps(value, indent=2, ensure_ascii=False))


def create_repository(root):
    root.mkdir(parents=True)
    (root / "billing.py").write_text(
        "def calculate_invoice_total(items):\n"
        "    return sum(item.price for item in items)\n",
        encoding="utf-8",
    )
    (root / "service.py").write_text(
        "from billing import calculate_invoice_total\n\n"
        "def create_invoice(items):\n"
        "    return calculate_invoice_total(items)\n",
        encoding="utf-8",
    )
    (root / "test_billing.py").write_text(
        "from billing import calculate_invoice_total\n\n"
        "def test_invoice_total():\n"
        "    assert calculate_invoice_total([]) == 0\n",
        encoding="utf-8",
    )


def build_agent(root, *, pressure_window=False):
    config = {
        "approval_policy": "auto",
        "verification_command": "",
        "max_new_tokens": 64,
    }
    if pressure_window:
        # This deliberately tiny window makes Context Pressure deterministic.
        config.update(
            {
                "provider_context_limit_tokens": 3500,
                "compaction_reserve_tokens": 1000,
                "compaction_keep_recent_tokens": 700,
            }
        )
    return Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(root),
        session_store=SessionStore(root / ".pico" / "sessions"),
        config=PicoConfig(**config),
    )


def activate(agent, run_id, goal):
    contract = TaskContract(
        goal=goal,
        task_kind="read_only",
        requires_workspace_change=False,
        requires_verification=False,
    )
    run_log = RunLog(
        run_id,
        f"task_{run_id}",
        agent.session.data["id"],
        agent.dependencies.run_store,
    )
    agent.run.run_log = run_log
    first = run_log.append_user(contract)
    agent.run.projection = RunProjection().apply_event(first)
    agent.run.execution_context = ExecutionContext.root(max_seconds=30)
    return run_log


def append_synthetic_historical_transaction(agent, index):
    """Create a large, protocol-valid history fixture without re-teaching Day 4."""
    run_log = agent.run.run_log
    call = ToolCall(
        "read_file",
        {"path": f"evidence_{index}.txt", "start_line": 1, "end_line": 20},
        f"call_history_{index}",
    )
    agent.apply_run_event(run_log.append_tool_call(call))
    agent.apply_run_event(
        run_log.append_tool_started(
            call,
            risky=False,
            effect_scope="none",
            potential_effects=[],
        )
    )
    outcome = ToolOutcome(
        tool_call_id=call.call_id,
        tool_name=call.name,
        status="success",
        execution_state="completed",
        side_effect_state="none",
        content=(f"historical fact {index}: " + "invoice evidence " * 220),
    )
    agent.apply_run_event(run_log.append_tool_result(outcome))


def call_transaction(agent, call_id):
    return [
        event.kind
        for event in agent.run.run_log.events
        if (event.kind == "assistant_tool_call" and event.call_id == call_id)
        or (event.kind == "tool_started" and event.call_id == call_id)
        or (event.kind == "tool_result" and event.call_id == call_id)
    ]


def repo_map_section(input_text):
    start = "Repository map (task-ranked Python signatures; use read_file for details):"
    section = input_text.split(start, 1)[1].split("\n\nRun working state:", 1)[0]
    return start + section


def fallback_entry_headers(history):
    headers = []
    for line in str(history).splitlines():
        if line.startswith("[assistant/tool]"):
            headers.append(line)
        elif line.startswith("[tool/"):
            headers.append(line.split("]", 1)[0] + "]")
    return headers


def durable_kind_counts(events):
    return {
        kind: sum(event.kind == kind for event in events)
        for kind in (
            "assistant_tool_call",
            "tool_started",
            "tool_result",
            "compaction",
        )
    }


def default_context_experiment(root):
    create_repository(root)
    agent = build_agent(root)
    agent.dependencies.project_memory.store(
        action="create",
        filename=MEMORY_FILENAME,
        name="Invoice verification procedure",
        description="How invoice changes should be checked.",
        memory_type="reference",
        content=MEMORY_LITERAL,
        source_run_id="bootstrap",
    )
    run_log = activate(agent, "run_day5_default", "Inspect invoice totals")

    prompt, metadata = agent.prompt.build(QUERY)
    input_text = prompt.input_text
    rendered_repo_map = repo_map_section(input_text)

    recall_call = ToolCall(
        "memory_recall",
        {"filenames": [MEMORY_FILENAME]},
        "call_memory_recall",
    )
    agent.apply_run_event(run_log.append_tool_call(recall_call))
    recall_outcome = agent.tools.execute(recall_call)
    recall_transaction = call_transaction(agent, recall_call.call_id)

    assert agent.dependencies.project_memory is not None
    assert agent.dependencies.repo_map is not None
    assert metadata["compaction"] is None
    assert all(event.kind != "compaction" for event in run_log.events)
    assert MEMORY_FILENAME in input_text
    assert MEMORY_LITERAL not in input_text
    assert "calculate_invoice_total" in rendered_repo_map
    assert MEMORY_LITERAL in recall_outcome.content
    assert recall_transaction == [
        "assistant_tool_call",
        "tool_started",
        "tool_result",
    ]

    print_section(
        "A. 默认上下文增强（没有 Context Pressure）",
        {
            "default_components": {
                "project_memory": type(agent.dependencies.project_memory).__name__,
                "repo_map": type(agent.dependencies.repo_map).__name__,
            },
            "provider_context_limit_tokens": (
                agent.config.provider_context_limit_tokens
            ),
            "compaction_metadata": metadata["compaction"],
            "repo_map_section": rendered_repo_map,
            "memory": {
                "catalog_contains_filename": MEMORY_FILENAME in input_text,
                "prompt_contains_full_card": MEMORY_LITERAL in input_text,
                "recall_transaction": recall_transaction,
                "recall_status": recall_outcome.status,
                "recall_contains_full_card": MEMORY_LITERAL in recall_outcome.content,
            },
        },
    )


def build_pressure_fixture(root, run_id):
    create_repository(root)
    agent = build_agent(root, pressure_window=True)
    run_log = activate(agent, run_id, "Inspect invoice history under pressure")
    for index in range(6):
        append_synthetic_historical_transaction(agent, index)
    return agent, run_log


def bounded_fallback_experiment(root):
    agent, run_log = build_pressure_fixture(root, "run_day5_fallback")
    original_events = tuple(run_log.events)
    original_ids = [event.event_id for event in original_events]

    compaction, history_override = agent.prompt.prepare_compaction(
        QUERY,
        provider_context_tokens=3400,
    )
    prompt, prompt_metadata = agent.prompt.build(
        QUERY,
        provider_context_tokens=3400,
        compaction_metadata=compaction,
        history_override=history_override,
    )
    physical_events = tuple(agent.dependencies.run_store.read_events(run_log.run_id))
    headers = fallback_entry_headers(history_override)
    physical_counts = durable_kind_counts(physical_events)

    assert compaction["mode"] == "runtime_recent_transactions"
    assert compaction["degraded"] is True
    assert compaction["committed"] is False
    assert compaction["failure_code"] == "semantic_summary_unavailable"
    assert compaction["selected_count"] == 2
    assert len(headers) == 2
    assert headers[0].startswith("[assistant/tool]")
    assert headers[1].startswith("[tool/read_file/success/none]")
    assert "tool_started" not in history_override
    assert [event.event_id for event in physical_events] == original_ids
    assert all(event.kind != "compaction" for event in physical_events)
    assert physical_counts == {
        "assistant_tool_call": 6,
        "tool_started": 6,
        "tool_result": 6,
        "compaction": 0,
    }
    assert run_log.generation == 1
    assert prompt_metadata["within_budget"] is True
    assert "Current run events (bounded fallback):" in prompt.input_text

    print_section(
        "B. Context Pressure：失败后保留完整 Call/Result 对",
        {
            "teaching_window": {
                "total": agent.config.provider_context_limit_tokens,
                "reserve": agent.config.compaction_reserve_tokens,
                "keep_recent": agent.config.compaction_keep_recent_tokens,
                "trigger_tokens": compaction["trigger_context_tokens"],
                "trigger_threshold": compaction["trigger_threshold_tokens"],
            },
            "compaction": compaction,
            "model_visible_history_view": {
                "retained_call_result_pairs": 1,
                "entries": headers,
            },
            "physical_log": {
                "complete_tool_transactions": physical_counts["tool_result"],
                "durable_counts": physical_counts,
            },
            "tool_started_visibility": (
                "durable in events.jsonl; intentionally absent from Prompt History"
            ),
            "physical_event_count_before": len(original_events),
            "physical_event_count_after": len(physical_events),
            "physical_log_was_unchanged": [event.event_id for event in physical_events]
            == original_ids,
        },
    )


class DeterministicSummarizer:
    def __init__(self):
        self.calls = []
        self.seen_event_kinds = []

    def summarize(self, events, **_kwargs):
        self.seen_event_kinds = [event.kind for event in events]
        self.calls.append(
            {
                "duration_ms": 0,
                "completion_metadata": {"fixture": "deterministic"},
            }
        )
        return (
            "## Progress\n"
            "### Done\n"
            f"- {SUMMARY_MARKER}\n\n"
            "## Critical Context\n"
            "- invoice history was inspected"
        )


def semantic_compaction_experiment(root):
    agent, run_log = build_pressure_fixture(root, "run_day5_semantic")
    summarizer = DeterministicSummarizer()
    agent.prompt.context.semantic_summarizer = summarizer
    original_physical = tuple(run_log.events)
    original_ids = [event.event_id for event in original_physical]
    original_history_view_count = len(run_log.active_events())

    compaction, history_override = agent.prompt.prepare_compaction(
        QUERY,
        provider_context_tokens=3400,
    )
    prompt, prompt_metadata = agent.prompt.build(
        QUERY,
        provider_context_tokens=3400,
        compaction_metadata=compaction,
        history_override=history_override,
    )
    physical_after = tuple(agent.dependencies.run_store.read_events(run_log.run_id))
    history_view_after = tuple(run_log.active_events())
    physical_counts = durable_kind_counts(physical_after)

    assert compaction["mode"] == "semantic_history"
    assert compaction["degraded"] is False
    assert compaction["committed"] is True
    assert history_override is None
    assert run_log.generation == 2
    assert SUMMARY_MARKER in prompt.input_text
    assert prompt_metadata["history_projection"]["projection_mode"] == (
        "compacted_complete_transactions"
    )
    assert len(history_view_after) < original_history_view_count
    assert history_view_after[0].kind == "compaction"
    assert len(physical_after) == len(original_physical) + 1
    assert [event.event_id for event in physical_after[: len(original_ids)]] == (
        original_ids
    )
    assert physical_after[-1].kind == "compaction"
    assert any(event.kind == "tool_started" for event in physical_after)
    assert all(event.kind != "tool_started" for event in history_view_after)
    assert physical_counts == {
        "assistant_tool_call": 6,
        "tool_started": 6,
        "tool_result": 6,
        "compaction": 1,
    }

    print_section(
        "C. Context Pressure：Semantic Summary 成功提交",
        {
            "compaction": compaction,
            "summary_marker_in_prompt": SUMMARY_MARKER in prompt.input_text,
            "summarizer_input_kinds": summarizer.seen_event_kinds,
            "generation": run_log.generation,
            "active_history_view": {
                "meaning": "model-visible RunLog History View, not RunProjection",
                "before_count": original_history_view_count,
                "after_count": len(history_view_after),
                "after_kinds": [event.kind for event in history_view_after],
            },
            "physical_log": {
                "before_count": len(original_physical),
                "after_count": len(physical_after),
                "complete_tool_transactions": physical_counts["tool_result"],
                "original_event_ids_remain_prefix": [
                    event.event_id for event in physical_after[: len(original_ids)]
                ]
                == original_ids,
                "durable_counts": physical_counts,
            },
            "history_projection": prompt_metadata["history_projection"],
        },
    )


def main():
    with tempfile.TemporaryDirectory(prefix="pico-day5-") as directory:
        root = Path(directory)
        default_context_experiment(root / "default")
        bounded_fallback_experiment(root / "fallback")
        semantic_compaction_experiment(root / "semantic")


if __name__ == "__main__":
    main()
