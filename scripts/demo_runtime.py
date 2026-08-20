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
                ModelAction.tool("read_file", {"path": "sample.txt", "start": 1, "end": 20}),
                ModelAction.tool("patch_file", {
                    "path": "sample.txt", "old_text": "alpha", "new_text": "beta",
                    "expected_revision": revision,
                }),
                ModelAction.final("Updated sample.txt."),
            ]),
            WorkspaceContext.build(root),
            SessionStore(root / ".pico/sessions"),
            config=PicoConfig(approval_policy="auto", verification_command=""),
        )
        answer = agent.ask("Read sample.txt and replace alpha with beta.")
        report = agent.build_report(agent.run.task_state)
        entries = agent.services.run_store.read_entries(agent.run.task_state.run_id)
        turns = [entry for entry in entries if entry.kind == "turn_metrics"]
        print(json.dumps({
            "answer": answer,
            "content": target.read_text(encoding="utf-8"),
            "provider_conversation_mode": agent.model_client.conversation_mode,
            "provider_prompt_reused": [
                entry.payload["prompt_reused"] for entry in turns
            ],
            "context_generation": agent.run.journal.generation,
            "journal_schema": entries[0].to_dict()["schema_version"],
            "evidence_effects": report["evidence"]["effects"],
            "journal_entry_count": len(entries),
            "pending_operations": agent.services.run_store.replay(
                agent.run.task_state.run_id
            ).summary()["pending_operations"],
            "run_dir": str(agent.run.run_dir),
        }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
