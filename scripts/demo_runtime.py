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

        assert catalog_in_initial_prompt is True
        assert recalled_card["included_filenames"] == [
            "reference_safe_edit.md"
        ]
        assert "Read the target revision" in recalled_card[
            "card_content_returned_to_model"
        ]
        assert outcome.status == "completed"
        assert replay_matches is True

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
                    "final_file": target.read_text(encoding="utf-8"),
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
