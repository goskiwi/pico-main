#!/usr/bin/env python3
"""Run compact native-action Runtime demonstrations."""

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
from pico.evidence import RunEvidence
from pico.mutations import file_revision


def main():
    with tempfile.TemporaryDirectory(prefix="pico-demo-") as directory:
        root = Path(directory)
        target = root / "sample.txt"
        target.write_text("alpha\n", encoding="utf-8")
        revision = file_revision(target)
        agent = Pico(
            FakeModelClient([
                ModelAction.tool("memory_recall", {
                    "filenames": ["reference_safe_edit.md"],
                }),
                ModelAction.tool("update_working_state", {
                    "add_constraints": ["Only edit sample.txt"],
                    "add_next_steps": ["Read sample.txt"],
                }),
                ModelAction.tool("read_file", {"path": "sample.txt", "start_line": 1, "end_line": 20}),
                ModelAction.tool("update_working_state", {
                    "add_decisions": ["The exact source text is alpha"],
                    "remove_next_steps": ["Read sample.txt"],
                    "add_next_steps": ["Replace alpha with beta"],
                }),
                ModelAction.tool("edit_file", {
                    "path": "sample.txt", "old_text": "alpha", "new_text": "beta",
                    "expected_revision": revision,
                }),
                ModelAction.tool("update_working_state", {
                    "remove_next_steps": ["Replace alpha with beta"],
                }),
                ModelAction.final("Updated sample.txt."),
            ]),
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
        answer = agent.ask(
            "Read sample.txt and replace alpha with beta.",
            task_kind="modify",
            requires_workspace_change=True,
            requires_verification=False,
        )
        run_id = agent.run.projection.run_id
        events = agent.dependencies.run_store.read_events(run_id)
        projection = agent.dependencies.run_store.replay(run_id)
        evidence = RunEvidence.from_events(events)
        turns = [entry for entry in events if entry.kind == "turn_metrics"]
        recall_calls = [
            entry
            for entry in events
            if entry.kind == "assistant_tool_call" and entry.name == "memory_recall"
        ]
        recall_results = {
            entry.call_id: entry
            for entry in events
            if entry.kind == "tool_result" and entry.name == "memory_recall"
        }
        tool_calls = [
            entry for entry in events if entry.kind == "assistant_tool_call"
        ]
        tool_started = {
            entry.call_id: entry for entry in events if entry.kind == "tool_started"
        }
        tool_results = {
            entry.call_id: entry for entry in events if entry.kind == "tool_result"
        }
        print(json.dumps({
            "answer": answer,
            "completion": {
                "status": agent.run.task.lifecycle.status,
                "stop_reason": agent.run.task.lifecycle.stop_reason,
            },
            "content": target.read_text(encoding="utf-8"),
            "provider_conversation_mode": agent.model_client.conversation_mode,
            "provider_prompt_reused": [
                entry.payload["prompt_reused"] for entry in turns
            ],
            "context_generation": agent.run.run_log.generation,
            "run_log_schema": events[0].to_dict()["schema_version"],
            "evidence_effects": evidence.effects,
            "working_state": projection.task.working.to_dict(),
            "memory_recalls": [
                {
                    "filenames": call.args["filenames"],
                    "status": recall_results[call.call_id].outcome_status,
                }
                for call in recall_calls
            ],
            "tool_transactions": [
                {
                    "tool": call.name,
                    "call_id": call.call_id,
                    "events": [
                        call.kind,
                        tool_started[call.call_id].kind,
                        tool_results[call.call_id].kind,
                    ],
                    "status": tool_results[call.call_id].outcome_status,
                    "side_effect_state": tool_results[
                        call.call_id
                    ].side_effect_state,
                    "affected_paths": list(
                        tool_results[call.call_id].affected_paths
                    ),
                }
                for call in tool_calls
            ],
            "run_event_count": len(events),
            "pending_call_id": projection.summary()["pending_call_id"],
            "run_dir": str(agent.dependencies.run_store.run_dir(run_id)),
        }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
