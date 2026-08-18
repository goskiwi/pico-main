from pico.contracts import ToolOutcome
from pico.evidence import EvidenceLedger


def changed_outcome():
    return ToolOutcome(
        tool_call_id="call_1",
        tool_name="patch_file",
        status="ok",
        execution_state="completed",
        side_effect_state="changed",
        content="patched",
        call_fingerprint="fingerprint",
        admission={"status": "admitted", "stages": []},
        affected_paths=("src/app.py",),
        workspace_fingerprint="workspace-before",
        artifact_id="artifact_1",
        metadata={"effect_scope": "workspace"},
    )


def test_live_and_event_recovery_build_identical_evidence():
    outcome = changed_outcome()
    live = EvidenceLedger()
    live.record_tool(outcome, "workspace-after")
    restored = EvidenceLedger.from_events(
        [
            {
                "event_type": "operation_finished",
                "payload": {
                    "content_workspace_fingerprint": "workspace-after",
                    "outcome": outcome.to_dict(),
                },
            }
        ]
    )

    assert restored.to_dict() == live.to_dict()


def test_workspace_fact_invalidates_current_verification():
    ledger = EvidenceLedger()
    ledger.record_verification(
        {
            "verification_id": "verify_1",
            "status": "passed",
            "freshness": "current",
            "workspace_fingerprint": "workspace-before",
        }
    )

    ledger.record_tool(changed_outcome(), "workspace-after")

    assert ledger.verifications[0]["freshness"] == "stale"
    assert ledger.verifications[0]["invalidated_by"] == "call_1"
