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


def tool_events(agent, call_id):
    return [
        event
        for event in agent.run.run_log.events
        if (event.kind == "assistant_tool_call" and event.call_id == call_id)
        or (event.kind == "tool_started" and event.call_id == call_id)
        or (event.kind == "tool_result" and event.call_id == call_id)
    ]


def result_event(agent, call_id):
    return next(
        event
        for event in agent.run.run_log.events
        if event.kind == "tool_result" and event.call_id == call_id
    )


def started_event(agent, call_id):
    return next(
        event
        for event in agent.run.run_log.events
        if event.kind == "tool_started" and event.call_id == call_id
    )


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
                "derived_correction_action": outcome.correction_action,
                "affected_paths": list(event.affected_paths),
                "artifact_id": outcome.artifact_id,
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
        run_outcome = repaired._ask_with_intent(
            "Replace alpha without losing concurrent edits",
            intent="modify",
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
                    ModelAction.final(
                        "The policy denied the conditional write, so no file was created."
                    ),
                ]
            ),
            workspace=WorkspaceContext.build(denied_root),
            session_store=SessionStore(denied_root / ".pico" / "sessions"),
            config=PicoConfig(
                approval_policy="deny",
                verification_command="",
            ),
        )
        denied_run_outcome = denied._ask_with_intent(
            "Attempt to create created.txt only if policy permits it; otherwise report the denial.",
            intent="modify_optional",
        )
        denied_outcomes = outcomes(denied)
        denied_started = [
            event.call_id
            for event in denied.run.run_log.events
            if event.kind == "tool_started"
        ]

        stale_result_event = result_event(repaired, "call_edit_stale")
        stale_outcome = ToolOutcome.from_dict(stale_result_event.payload["outcome"])
        repaired_started_event = started_event(repaired, "call_edit_repaired")
        repaired_result_event = result_event(repaired, "call_edit_repaired")
        repaired_outcome = ToolOutcome.from_dict(
            repaired_result_event.payload["outcome"]
        )
        potential_effect = repaired_started_event.payload["potential_effects"][0]
        transition = repaired_outcome.structured["path_transitions"][0]
        preimage_id = potential_effect["before_artifact_id"]
        preimage_text = repaired.dependencies.artifacts.read_internal_text(
            repaired.run.projection.run_id,
            preimage_id,
        )
        repaired_transaction = [
            event.kind for event in tool_events(repaired, "call_edit_repaired")
        ]

        denied_result_event = result_event(denied, "call_denied_write")
        denied_tool_outcome = ToolOutcome.from_dict(
            denied_result_event.payload["outcome"]
        )
        denied_transaction = [
            event.kind for event in tool_events(denied, "call_denied_write")
        ]

        assert run_outcome.answer == "Concurrent edit preserved and repair completed."
        assert target.read_text(encoding="utf-8") == "agent\nexternal\n"
        by_id = {item["call_id"]: item for item in repair_outcomes}
        assert by_id["call_edit_stale"]["failure"] == "revision_conflict"
        assert by_id["call_edit_stale"]["side_effect_state"] == "none"
        assert by_id["call_edit_repaired"]["status"] == "success"
        assert by_id["call_edit_repaired"]["affected_paths"] == ["subject.txt"]
        assert set(repaired_result_event.payload) == {"outcome"}
        assert repaired_result_event.payload["outcome"]["tool_call_id"] == (
            "call_edit_repaired"
        )
        assert repaired_result_event.payload["outcome"]["tool_name"] == "edit_file"
        assert "artifact_id" in repaired_result_event.payload["outcome"]
        assert "correction_action" not in repaired_result_event.payload["outcome"]
        assert repaired_transaction == [
            "assistant_tool_call",
            "tool_started",
            "tool_result",
        ]
        assert stale_outcome.structured["expected_revision"] == initial_revision
        assert stale_outcome.structured["actual_revision"] == external_revision
        assert potential_effect["path"] == "subject.txt"
        assert potential_effect["before_state"] == external_revision
        assert preimage_id.startswith("preimage_")
        assert preimage_text == external_content
        assert transition == {
            "path": "subject.txt",
            "before_state": external_revision,
            "after_state": file_revision(target),
            "before_artifact_id": preimage_id,
        }
        assert "--- a/subject.txt" in repaired_outcome.content
        assert "+++ b/subject.txt" in repaired_outcome.content
        assert "-alpha" in repaired_outcome.content
        assert "+agent" in repaired_outcome.content
        assert denied_run_outcome.answer == (
            "The policy denied the conditional write, so no file was created."
        )
        assert denied_outcomes[0]["failure"] == "approval_denied"
        assert denied_outcomes[0]["execution_state"] == "not_started"
        assert denied_transaction == ["assistant_tool_call", "tool_result"]
        assert set(denied_result_event.payload) == {"outcome"}
        assert denied_tool_outcome.structured == {}
        assert denied_tool_outcome.artifact_id == ""
        assert "preimage_" not in json.dumps(denied_result_event.payload)
        assert denied_started == []
        assert not (denied_root / "created.txt").exists()

        print_section(
            "Run Log v16 与安全 Edit",
            {
                "answer": run_outcome.answer,
                "final_content": target.read_text(encoding="utf-8"),
                "successful_transaction": repaired_transaction,
                "tool_result_top_level_keys": sorted(repaired_result_event.payload),
                "canonical_outcome_identity": {
                    "tool_call_id": repaired_outcome.tool_call_id,
                    "tool_name": repaired_outcome.tool_name,
                    "artifact_id": repaired_outcome.artifact_id,
                },
                "derived_not_persisted": {
                    "correction_action": repaired_outcome.correction_action,
                    "stored_in_outcome": "correction_action"
                    in repaired_result_event.payload["outcome"],
                },
                "stale_revision_conflict": {
                    "expected_revision": stale_outcome.structured["expected_revision"],
                    "actual_revision": stale_outcome.structured["actual_revision"],
                    "recommended_next_tool": stale_outcome.structured[
                        "recommended_next_tool"
                    ],
                },
                "successful_edit_evidence": {
                    "tool_started_potential_effect": potential_effect,
                    "tool_result_path_transition": transition,
                    "preimage_text": preimage_text,
                    "runner_receipt_and_unified_diff": repaired_outcome.content,
                },
                "tool_outcomes": repair_outcomes,
            },
        )
        print_section(
            "Approval deny",
            {
                "answer": denied_run_outcome.answer,
                "transaction": denied_transaction,
                "tool_result_top_level_keys": sorted(denied_result_event.payload),
                "tool_outcomes": denied_outcomes,
                "tool_started_events": denied_started,
                "preimage_present": "preimage_"
                in json.dumps(denied_result_event.payload),
                "file_exists": (denied_root / "created.txt").exists(),
            },
        )


if __name__ == "__main__":
    main()
