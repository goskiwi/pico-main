"""Day 4: exercise approval, revision conflicts, repair, and tool auditing."""

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
from pico.contracts import ToolOutcome
from pico.mutations import content_revision, file_revision


def print_section(title, value):
    print(f"\n=== {title} ===")
    print(json.dumps(value, indent=2, ensure_ascii=False))


def outcomes(agent):
    rows = []
    for event in agent.run.run_log.events:
        if event.kind != "tool_result":
            continue
        outcome = ToolOutcome.from_dict(event.payload["outcome"])
        rows.append(
            {
                "call_id": event.call_id,
                "tool": event.name,
                "status": event.outcome_status,
                "execution_state": outcome.execution_state,
                "side_effect_state": event.side_effect_state,
                "failure": outcome.failure.code if outcome.failure else "",
                "correction_action": outcome.correction_action,
                "affected_paths": list(event.affected_paths),
            }
        )
    return rows


def main():
    with tempfile.TemporaryDirectory(prefix="pico-day4-") as directory:
        root = Path(directory)
        target = root / "subject.txt"
        target.write_text("alpha\n", encoding="utf-8")
        initial_revision = file_revision(target)
        external_content = "alpha\nexternal\n"
        external_revision = content_revision(external_content.encode("utf-8"))

        class DriftBeforeFirstEditClient(FakeModelClient):
            request_count = 0

            def complete_action(self, *args, **kwargs):
                self.request_count += 1
                if self.request_count == 2:
                    target.write_text(external_content, encoding="utf-8")
                return super().complete_action(*args, **kwargs)

        repair_model = DriftBeforeFirstEditClient(
            [
                ModelAction.tool(
                    "read_file",
                    {"path": "subject.txt", "start_line": 1, "end_line": 20},
                    call_id="call_read_initial",
                ),
                ModelAction.tool(
                    "edit_file",
                    {
                        "path": "subject.txt",
                        "old_text": "alpha\n",
                        "new_text": "agent\n",
                        "expected_revision": initial_revision,
                    },
                    call_id="call_edit_stale",
                ),
                ModelAction.tool(
                    "read_file",
                    {"path": "subject.txt", "start_line": 1, "end_line": 20},
                    call_id="call_read_current",
                ),
                ModelAction.tool(
                    "edit_file",
                    {
                        "path": "subject.txt",
                        "old_text": "alpha\n",
                        "new_text": "agent\n",
                        "expected_revision": external_revision,
                    },
                    call_id="call_edit_repaired",
                ),
                ModelAction.final("Concurrent edit preserved and repair completed."),
            ]
        )
        repaired = Pico(
            model_client=repair_model,
            workspace=WorkspaceContext.build(root),
            session_store=SessionStore(root / ".pico" / "sessions"),
            config=PicoConfig(
                approval_policy="auto",
                verification_command="",
            ),
        )
        answer = repaired.ask(
            "Replace alpha without losing concurrent edits",
            task_kind="modify",
            requires_workspace_change=True,
            requires_verification=False,
        )
        repair_outcomes = outcomes(repaired)

        denied_root = root / "denied"
        denied_root.mkdir()
        denied = Pico(
            model_client=FakeModelClient(
                [
                    ModelAction.tool(
                        "write_file",
                        {
                            "path": "created.txt",
                            "content": "must not be written\n",
                        },
                        call_id="call_denied_write",
                    ),
                    ModelAction.final("Denied write was not executed."),
                ]
            ),
            workspace=WorkspaceContext.build(denied_root),
            session_store=SessionStore(denied_root / ".pico" / "sessions"),
            config=PicoConfig(
                approval_policy="deny",
                verification_command="",
            ),
        )
        denied_answer = denied.ask(
            "Create created.txt",
            task_kind="modify",
            requires_workspace_change=False,
            requires_verification=False,
        )
        denied_outcomes = outcomes(denied)
        denied_started = [
            event.call_id
            for event in denied.run.run_log.events
            if event.kind == "tool_started"
        ]

        assert answer == "Concurrent edit preserved and repair completed."
        assert target.read_text(encoding="utf-8") == "agent\nexternal\n"
        by_id = {item["call_id"]: item for item in repair_outcomes}
        assert by_id["call_edit_stale"]["failure"] == "revision_conflict"
        assert by_id["call_edit_stale"]["side_effect_state"] == "none"
        assert by_id["call_edit_repaired"]["status"] == "success"
        assert by_id["call_edit_repaired"]["affected_paths"] == [
            "subject.txt"
        ]
        assert denied_answer == "Denied write was not executed."
        assert denied_outcomes[0]["failure"] == "approval_denied"
        assert denied_outcomes[0]["execution_state"] == "not_started"
        assert denied_started == []
        assert not (denied_root / "created.txt").exists()

        print_section(
            "并发修改后的修复",
            {
                "answer": answer,
                "final_content": target.read_text(encoding="utf-8"),
                "tool_outcomes": repair_outcomes,
            },
        )
        print_section(
            "Approval deny",
            {
                "answer": denied_answer,
                "tool_outcomes": denied_outcomes,
                "tool_started_events": denied_started,
                "file_exists": (denied_root / "created.txt").exists(),
            },
        )


if __name__ == "__main__":
    main()
