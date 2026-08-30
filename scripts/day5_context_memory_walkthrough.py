"""Day 5: inspect RepoMap, bounded context, memory recall, and compaction."""

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
from pico.context_manager import ContextManager
from pico.execution import ExecutionContext
from pico.run_log import RunLog
from pico.run_projection import RunProjection
from pico.task_state import TaskContract


def print_section(title, value):
    print(f"\n=== {title} ===")
    print(json.dumps(value, indent=2, ensure_ascii=False))


def append_historical_read(agent, index):
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
    agent.apply_run_event(
        run_log.append_tool_result(outcome)
    )


def main():
    with tempfile.TemporaryDirectory(prefix="pico-day5-") as directory:
        root = Path(directory)
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

        agent = Pico(
            model_client=FakeModelClient([]),
            workspace=WorkspaceContext.build(root),
            session_store=SessionStore(root / ".pico" / "sessions"),
            config=PicoConfig(
                approval_policy="auto",
                verification_command="",
                max_new_tokens=64,
            ),
        )
        memory_filename = "reference_invoice_checks.md"
        memory_literal = "RUN_ONLY_THE_TARGETED_INVOICE_TEST_FIRST"
        agent.dependencies.project_memory.store(
            action="create",
            filename=memory_filename,
            name="Invoice verification procedure",
            description="How invoice changes should be checked.",
            memory_type="reference",
            content=memory_literal,
            source_run_id="bootstrap",
        )

        contract = TaskContract(
            goal="Inspect how invoice totals are calculated",
            task_kind="read_only",
            requires_workspace_change=False,
            requires_verification=False,
        )
        run_log = RunLog(
            "run_day5",
            "task_day5",
            agent.session.data["id"],
            agent.dependencies.run_store,
        )
        agent.run.run_log = run_log
        first = run_log.append_user(contract)
        agent.run.projection = RunProjection().apply_event(first)
        agent.run.execution_context = ExecutionContext.root(max_seconds=30)
        for index in range(6):
            append_historical_read(agent, index)

        event_count_before = len(run_log.events)
        manager = ContextManager(
            agent,
            total_budget=3500,
            compaction_reserve_tokens=1000,
            compaction_keep_recent_tokens=350,
        )
        compaction, history_override = manager.prepare_compaction(
            "Where is calculate_invoice_total used?",
            provider_context_tokens=3400,
        )
        prompt, metadata = manager.build(
            "Where is calculate_invoice_total used?",
            provider_context_tokens=3400,
            compaction_metadata=compaction,
            history_override=history_override,
        )
        repo_map_lines = [
            line
            for line in prompt.splitlines()
            if "billing" in line.lower() or "calculate_invoice_total" in line
        ][:12]

        recall_call = ToolCall(
            "memory_recall",
            {"filenames": [memory_filename]},
            "call_memory_recall",
        )
        agent.apply_run_event(run_log.append_tool_call(recall_call))
        recall_outcome = agent.tools.execute(recall_call)

        assert memory_filename in prompt
        assert memory_literal not in prompt
        assert memory_literal in recall_outcome.content
        assert "calculate_invoice_total" in prompt
        assert metadata["within_budget"] is True
        assert metadata["compaction"] is not None
        assert metadata["compaction"]["trigger_context_tokens"] >= 3400
        if metadata["compaction"]["committed"]:
            assert any(event.kind == "compaction" for event in run_log.events)
            assert len(run_log.active_events()) < event_count_before
        else:
            assert metadata["compaction"]["degraded"] is True
            assert all(event.kind != "compaction" for event in run_log.events)

        print_section(
            "Prompt sections 与 RepoMap",
            {
                "section_order": metadata["section_order"],
                "section_tokens": metadata["sections"],
                "repo_map_matches": repo_map_lines,
                "within_budget": metadata["within_budget"],
            },
        )
        print_section(
            "Memory Catalog 与显式 Recall",
            {
                "catalog_contains_filename": memory_filename in prompt,
                "prompt_contains_full_card": memory_literal in prompt,
                "recall_status": recall_outcome.status,
                "recall_contains_full_card": memory_literal
                in recall_outcome.content,
            },
        )
        print_section(
            "Run Log Compaction",
            {
                "events_before": event_count_before,
                "events_after": len(run_log.events),
                "active_events_after": len(run_log.active_events()),
                "generation": run_log.generation,
                "metadata": metadata["compaction"],
                "original_events_still_on_disk": len(
                    agent.dependencies.run_store.read_events(run_log.run_id)
                ),
            },
        )


if __name__ == "__main__":
    main()
