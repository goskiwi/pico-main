"""Day 5: separate RepoMap context from Context Pressure."""

import json
import re
import tempfile
from html import unescape
from pathlib import Path

from pico import (
    FakeModelClient,
    Pico,
    PicoConfig,
    SessionStore,
    ToolCall,
    ToolOutcome,
    Workspace,
)
from pico.execution import ExecutionContext
from pico.run_log import RunLog
from pico.run_projection import RunProjection
from pico.task_state import TaskContract

QUERY = "Where is calculate_invoice_total used?"
SUMMARY_MARKER = "SEMANTIC-SUMMARY-MARKER"
WORKING_CONSTRAINT = "Prefer the targeted invoice test first"
WORKING_DECISION = "calculate_invoice_total is called by create_invoice"
WORKING_NEXT_STEP = "Inspect the newest invoice evidence"


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
        "mode": "auto",
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
        workspace=Workspace.build(root),
        session_store=SessionStore(root / ".pico" / "sessions"),
        config=PicoConfig(**config),
    )


def activate(agent, run_id, goal):
    contract = TaskContract(
        goal=goal,
        allows_workspace_mutation=False,
        verify_changes=False,
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


def untrusted_context(input_text):
    marker = '<untrusted_context trust="untrusted_data">\n'
    if marker not in input_text:
        return {}
    body = input_text.split(marker, 1)[1].split(
        "\n</untrusted_context>",
        1,
    )[0]
    return {
        name: unescape(value)
        for name, value in re.findall(
            r'<section name="([a-z_]+)">\n(.*?)\n</section>',
            body,
            flags=re.DOTALL,
        )
    }


def semantic_section(summary, title):
    marker = f"## {title}\n"
    content = str(summary).split(marker, 1)[1]
    return content.split("\n\n## ", 1)[0].strip()


def effective_recovery_context(agent, summary):
    """Compose a teaching view without creating Prompt or durable state."""
    projection = agent.run.projection
    task = projection.task
    working = task.working
    evidence = projection.evidence.to_dict()
    categories = {
        "Goal": {
            "source": "TaskContract from the first user_message",
            "value": task.contract.goal,
            "semantic_llm_generated": False,
        },
        "Constraints & Preferences": {
            "source": (
                "RunProjection.task.working.constraints from successful "
                "update_working_state Tool transactions"
            ),
            "value": list(working.constraints),
            "semantic_llm_generated": False,
        },
        "Progress": {
            "source": "Compaction Fact content: ## Progress",
            "value": semantic_section(summary, "Progress"),
            "semantic_llm_generated": True,
        },
        "Key Decisions": {
            "source": (
                "RunProjection.task.working.decisions from successful "
                "update_working_state Tool transactions"
            ),
            "value": list(working.decisions),
            "semantic_llm_generated": False,
        },
        "Next Steps": {
            "source": (
                "RunProjection.task.working.next_steps from successful "
                "update_working_state Tool transactions"
            ),
            "value": list(working.next_steps),
            "semantic_llm_generated": False,
        },
        "Critical Context": {
            "source": "Compaction Fact content: ## Critical Context",
            "value": semantic_section(summary, "Critical Context"),
            "semantic_llm_generated": True,
        },
        "Execution Evidence": {
            "source": (
                "RunEvidence projected from durable Tool Result and Verification Facts"
            ),
            "value": {
                "successful_observation_count": evidence[
                    "successful_observation_count"
                ],
                "changed_paths": evidence["change_set"]["net_changed_paths"],
                "verification_count": len(evidence["verifications"]),
            },
            "semantic_llm_generated": False,
        },
    }
    return {
        "view_kind": "teaching_observability_composition",
        "semantic_llm_generated_categories": [
            "Progress",
            "Critical Context",
        ],
        "persisted_as_one_view": False,
        "sent_as_seven_section_prompt": False,
        "used_by_completion_controller": False,
        "categories": categories,
    }


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


def repo_map_experiment(root):
    create_repository(root)
    agent = build_agent(root)
    run_log = activate(agent, "run_day5_default", QUERY)

    prompt, metadata = agent.prompt.build(QUERY)
    input_text = prompt.input_text
    context = untrusted_context(input_text)
    rendered_repo_map = context["repo_map"]

    assert agent.dependencies.repo_map is not None
    assert metadata["compaction"] is None
    assert all(event.kind != "compaction" for event in run_log.events)
    assert metadata["section_order"] == [
        "runtime_policy",
        "task_request",
        "untrusted_context",
    ]
    assert metadata["included_context_sections"] == [
        "workspace",
        "repo_map",
    ]
    assert "calculate_invoice_total" in rendered_repo_map

    print_section(
        "A. RepoMap 上下文（没有 Context Pressure）",
        {
            "repo_map": type(agent.dependencies.repo_map).__name__,
            "provider_context_limit_tokens": (
                agent.config.provider_context_limit_tokens
            ),
            "compaction_metadata": metadata["compaction"],
            "minimal_input": {
                "section_order": metadata["section_order"],
                "included_context_sections": metadata["included_context_sections"],
                "empty_working_state_omitted": "working_state" not in context,
                "empty_history_omitted": "history" not in context,
                "latest_user_request_omitted": (
                    "latest_user_request" not in prompt.input_text
                ),
            },
            "repo_map_section": rendered_repo_map,
        },
    )


def build_pressure_fixture(root, run_id):
    create_repository(root)
    agent = build_agent(root, pressure_window=True)
    run_log = activate(agent, run_id, QUERY)
    state_call = ToolCall(
        "update_working_state",
        {
            "add_constraints": [WORKING_CONSTRAINT],
            "add_decisions": [WORKING_DECISION],
            "add_next_steps": [WORKING_NEXT_STEP],
        },
        f"call_state_{run_id}",
    )
    agent.apply_run_event(run_log.append_tool_call(state_call))
    state_outcome = agent.tools.execute_pending(state_call.call_id)
    assert state_outcome.status == "success"
    assert agent.run.task.working.constraints == (WORKING_CONSTRAINT,)
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
    context = untrusted_context(prompt.input_text)
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
        "assistant_tool_call": 7,
        "tool_started": 7,
        "tool_result": 7,
        "compaction": 0,
    }
    assert run_log.generation == 1
    assert prompt_metadata["within_budget"] is True
    assert "Current run events (bounded fallback):" in context["history"]
    assert context["working_state"].startswith("constraints:")

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
        self.seen_tool_names = []

    def summarize(self, events, **_kwargs):
        self.seen_event_kinds = [event.kind for event in events]
        self.seen_tool_names = [
            event.name for event in events if event.kind == "assistant_tool_call"
        ]
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
    agent.prompt.semantic_summarizer = summarizer
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
    compaction_event = physical_after[-1]
    summary = compaction_event.content
    summary_headings = [
        line.removeprefix("## ")
        for line in summary.splitlines()
        if line.startswith("## ")
    ]
    recovery_context = effective_recovery_context(agent, summary)

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
    assert summary_headings == ["Progress", "Critical Context"]
    assert "update_working_state" not in summarizer.seen_tool_names
    assert any(event.kind == "tool_started" for event in physical_after)
    assert all(event.kind != "tool_started" for event in history_view_after)
    assert physical_counts == {
        "assistant_tool_call": 7,
        "tool_started": 7,
        "tool_result": 7,
        "compaction": 1,
    }
    assert list(recovery_context["categories"]) == [
        "Goal",
        "Constraints & Preferences",
        "Progress",
        "Key Decisions",
        "Next Steps",
        "Critical Context",
        "Execution Evidence",
    ]
    assert recovery_context["semantic_llm_generated_categories"] == [
        "Progress",
        "Critical Context",
    ]
    assert recovery_context["persisted_as_one_view"] is False
    assert recovery_context["sent_as_seven_section_prompt"] is False
    assert recovery_context["used_by_completion_controller"] is False
    assert agent.run.task.working.constraints == (WORKING_CONSTRAINT,)
    assert agent.run.task.working.decisions == (WORKING_DECISION,)
    assert agent.run.task.working.next_steps == (WORKING_NEXT_STEP,)

    print_section(
        "C. Context Pressure：Semantic Summary 成功提交",
        {
            "compaction": compaction,
            "summary_marker_in_prompt": SUMMARY_MARKER in prompt.input_text,
            "semantic_summary_contract": {
                "headings": summary_headings,
                "llm_generated_section_count": len(summary_headings),
            },
            "summarizer_input_kinds": summarizer.seen_event_kinds,
            "summarizer_input_tool_names": summarizer.seen_tool_names,
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
            "effective_recovery_context": recovery_context,
        },
    )


def main():
    with tempfile.TemporaryDirectory(prefix="pico-day5-") as directory:
        root = Path(directory)
        repo_map_experiment(root / "repo-map")
        bounded_fallback_experiment(root / "fallback")
        semantic_compaction_experiment(root / "semantic")


if __name__ == "__main__":
    main()
