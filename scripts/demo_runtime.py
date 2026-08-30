#!/usr/bin/env python3
"""Demonstrate default Memory Catalog plus explicit Memory Recall."""

import json
import tempfile
from pathlib import Path

from pico import (
    FakeModelClient,
    ModelAction,
    Pico,
    PicoConfig,
    SessionStore,
    WorkspaceContext,
)
from pico.mutations import file_revision


def effective_recovery_context(projection, events, recalled_card, final_file):
    """Compose seven teaching categories from their real owners."""
    working = projection.task.working
    evidence = projection.evidence.to_dict()
    successful_results = [
        event.name
        for event in events
        if event.kind == "tool_result" and event.outcome_status == "success"
    ]
    categories = {
        "Goal": {
            "source": "TaskContract from the first user_message",
            "value": projection.task.contract.goal,
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
            "source": (
                "RunProjection lifecycle plus durable Tool Result Facts; "
                "no Semantic Compaction occurred"
            ),
            "value": {
                "run_status": projection.status,
                "successful_tool_results": successful_results,
            },
            "semantic_llm_generated": False,
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
            "source": (
                "current Workspace content plus explicit memory_recall "
                "Tool Result; no Semantic Compaction occurred"
            ),
            "value": {
                "sample.txt": final_file,
                "recalled_filenames": recalled_card["included_filenames"],
            },
            "semantic_llm_generated": False,
        },
        "Execution Evidence": {
            "source": (
                "RunEvidence projected from durable Tool Result and Verification Facts"
            ),
            "value": {
                "successful_observation_count": evidence[
                    "successful_observation_count"
                ],
                "change_set": evidence["change_set"],
                "verification_count": len(evidence["verifications"]),
            },
            "semantic_llm_generated": False,
        },
    }
    return {
        "view_kind": "teaching_observability_composition",
        "semantic_compaction_present": False,
        "semantic_llm_generated_categories": [],
        "persisted_as_one_view": False,
        "sent_as_seven_section_prompt": False,
        "used_by_completion_controller": False,
        "categories": categories,
    }


def main():
    with tempfile.TemporaryDirectory(prefix="pico-demo-") as directory:
        root = Path(directory)
        target = root / "sample.txt"
        target.write_text("alpha\n", encoding="utf-8")
        revision = file_revision(target)
        model = FakeModelClient(
            [
                ModelAction.tool(
                    "memory_recall",
                    {"filenames": ["reference_safe_edit.md"]},
                ),
                ModelAction.tool(
                    "update_working_state",
                    {
                        "add_constraints": ["Only edit sample.txt"],
                        "add_next_steps": ["Read sample.txt"],
                    },
                ),
                ModelAction.tool(
                    "read_file",
                    {"path": "sample.txt", "start_line": 1, "end_line": 20},
                ),
                ModelAction.tool(
                    "update_working_state",
                    {
                        "add_decisions": ["The exact source text is alpha"],
                        "remove_next_steps": ["Read sample.txt"],
                        "add_next_steps": ["Replace alpha with beta"],
                    },
                ),
                ModelAction.tool(
                    "edit_file",
                    {
                        "path": "sample.txt",
                        "old_text": "alpha",
                        "new_text": "beta",
                        "expected_revision": revision,
                    },
                ),
                ModelAction.tool(
                    "update_working_state",
                    {"remove_next_steps": ["Replace alpha with beta"]},
                ),
                ModelAction.final("Updated sample.txt."),
            ]
        )
        agent = Pico(
            model,
            WorkspaceContext.build(root),
            SessionStore(root / ".pico/sessions"),
            config=PicoConfig(approval_policy="auto", verification_command=""),
        )
        agent.dependencies.project_memory.store(
            action="create",
            filename="reference_safe_edit.md",
            name="Safe local edit procedure",
            description="How to make one revision-bound local text edit.",
            memory_type="reference",
            content="Read the target revision before applying one exact patch.",
            source_run_id="bootstrap",
        )
        outcome = agent.ask(
            "Read sample.txt and replace alpha with beta.",
            task_kind="modify",
            requires_workspace_change=True,
            requires_verification=False,
        )
        run_id = outcome.run_id
        events = agent.dependencies.run_store.read_events(run_id)
        projection = agent.dependencies.run_store.replay(run_id)
        recall_call = next(
            entry
            for entry in events
            if entry.kind == "assistant_tool_call" and entry.name == "memory_recall"
        )
        recall_result = next(
            entry
            for entry in events
            if entry.kind == "tool_result" and entry.call_id == recall_call.call_id
        )
        catalog_in_initial_prompt = "reference_safe_edit.md" in model.prompts[0]
        recalled_card = {
            "requested_filenames": list(recall_call.args["filenames"]),
            "included_filenames": recall_result.payload["outcome"]["structured"][
                "included_filenames"
            ],
            "status": recall_result.outcome_status,
            "card_content_returned_to_model": recall_result.payload["outcome"][
                "content"
            ],
        }
        working_state_updates = [
            dict(event.args)
            for event in events
            if event.kind == "assistant_tool_call"
            and event.name == "update_working_state"
        ]
        replay_matches = all(
            (
                outcome.run_id == projection.run_id,
                outcome.status == projection.status,
                outcome.answer == projection.final_answer,
                outcome.stop_reason == projection.stop_reason,
                outcome.final_diff == projection.final_diff,
                outcome.metrics == projection.metrics.to_dict(),
            )
        )
        final_file = target.read_text(encoding="utf-8")
        recovery_context = effective_recovery_context(
            projection,
            events,
            recalled_card,
            final_file,
        )

        assert catalog_in_initial_prompt is True
        assert recalled_card["included_filenames"] == ["reference_safe_edit.md"]
        assert (
            "Read the target revision"
            in recalled_card["card_content_returned_to_model"]
        )
        assert outcome.status == "completed"
        assert replay_matches is True
        assert len(working_state_updates) == 3
        assert all(event.kind != "compaction" for event in events)
        assert list(recovery_context["categories"]) == [
            "Goal",
            "Constraints & Preferences",
            "Progress",
            "Key Decisions",
            "Next Steps",
            "Critical Context",
            "Execution Evidence",
        ]
        assert recovery_context["semantic_llm_generated_categories"] == []
        assert recovery_context["persisted_as_one_view"] is False
        assert recovery_context["sent_as_seven_section_prompt"] is False
        assert recovery_context["used_by_completion_controller"] is False

        print(
            json.dumps(
                {
                    "demo_kind": "default Memory enhancement",
                    "memory_enhancement": {
                        "catalog_visible_in_initial_prompt": (
                            catalog_in_initial_prompt
                        ),
                        "recall": recalled_card,
                    },
                    "working_state_updates": working_state_updates,
                    "final_working_state": projection.task.working.to_dict(),
                    "effective_recovery_context": recovery_context,
                    "final_file": final_file,
                    "run_outcome": {
                        "run_id": outcome.run_id,
                        "status": outcome.status,
                        "answer": outcome.answer,
                        "stop_reason": outcome.stop_reason,
                        "final_diff": outcome.final_diff.to_dict(),
                    },
                    "replay_matches": replay_matches,
                },
                indent=2,
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
