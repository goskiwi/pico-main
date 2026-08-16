#!/usr/bin/env python3
"""Run compact native-action Runtime demonstrations."""

import json
import tempfile
from pathlib import Path

from pico import FakeModelClient, ModelAction, Pico, SessionStore, WorkspaceContext
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
            approval_policy="auto",
            verification_command="",
        )
        answer = agent.ask("Read sample.txt and replace alpha with beta.")
        report = agent.run_store.load_report(agent.current_task_state.run_id)
        print(json.dumps({
            "answer": answer,
            "content": target.read_text(encoding="utf-8"),
            "context_generation": agent.context_ledger.generation,
            "evidence_effects": report["evidence"]["effects"],
            "run_dir": str(agent.current_run_dir),
        }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
