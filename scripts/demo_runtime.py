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
                ModelAction.tool("read_file", {"path": "sample.txt", "start": 1, "end": 20}),
                ModelAction.tool("update_working_state", {
                    "add_decisions": ["The exact source text is alpha"],
                    "remove_next_steps": ["Read sample.txt"],
                    "add_next_steps": ["Replace alpha with beta"],
                }),
                ModelAction.tool("patch_file", {
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
        agent.services.project_memory.store(
            action="create",
            filename="reference_safe_edit.md",
            name="Safe local edit procedure",
            description="How to make one revision-bound local text edit.",
            memory_type="reference",
            content="Read the target revision before applying one exact patch.",
            source_session_id=agent.session.data["id"],
            source_run_id="bootstrap",
        )
        answer = agent.ask("Read sample.txt and replace alpha with beta.")
        report = agent.build_report(agent.run.task_state)
        events = agent.services.run_store.read_events(agent.run.task_state.run_id)
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
                "status": agent.run.task_state.status,
                "stop_reason": agent.run.task_state.stop_reason,
            },
            "content": target.read_text(encoding="utf-8"),
            "provider_conversation_mode": agent.model_client.conversation_mode,
            "provider_prompt_reused": [
                entry.payload["prompt_reused"] for entry in turns
            ],
            "context_generation": agent.run.run_log.generation,
            "run_log_schema": events[0].to_dict()["schema_version"],
            "evidence_effects": report["evidence"]["effects"],
            "working_state": report["working_state"],
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
            "pending_operations": agent.services.run_store.replay(
                agent.run.task_state.run_id
            ).summary()["pending_operations"],
            "run_dir": str(agent.services.run_store.run_dir(agent.run.task_state)),
        }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
