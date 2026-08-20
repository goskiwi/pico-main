from pico.contracts import ToolOutcome
from pico.evidence import EvidenceLedger
from pico.run_journal import JournalEntry


def changed_outcome():
    return ToolOutcome(
        tool_call_id="call_1",
        tool_name="patch_file",
        status="ok",
        execution_state="completed",
        side_effect_state="changed",
        content="patched",
        admission_status="admitted",
        affected_paths=("src/app.py",),
        effect_scope="workspace",
        artifact={"artifact_id": "artifact_1"},
    )


def entry(kind, payload, sequence=1):
    return JournalEntry(
        entry_id=f"run:entry:{sequence:06d}",
        sequence=sequence,
        run_id="run",
        task_id="task",
        session_id="session",
        kind=kind,
        timestamp="2026-01-01T00:00:00+00:00",
        payload=payload,
    )


def test_live_and_journal_recovery_build_identical_evidence():
    outcome = changed_outcome()
    journal_entry = entry(
        "tool_result",
        {
            "workspace_revision": 1,
            "outcome": outcome.to_dict(),
        },
    )
    live = EvidenceLedger()
    live.apply_entry(journal_entry)
    restored = EvidenceLedger.from_entries([journal_entry])

    assert restored.to_dict() == live.to_dict()


def test_workspace_fact_invalidates_current_verification():
    ledger = EvidenceLedger()
    ledger.apply_entry(
        entry(
            "verification_result",
            {
                "verification_id": "verify_1",
                "status": "passed",
                "freshness": "current",
                "workspace_fingerprint": "workspace-before",
            },
        )
    )

    ledger.apply_entry(
        entry(
            "tool_result",
            {
                "workspace_revision": 1,
                "outcome": changed_outcome().to_dict(),
            },
            sequence=2,
        )
    )

    assert ledger.verifications[0]["freshness"] == "stale"
    assert ledger.verifications[0]["invalidated_by"] == "call_1"


def effect(call_id, status, side_effect_state, paths, scope="workspace"):
    return {
        "tool_call_id": call_id,
        "status": status,
        "side_effect_state": side_effect_state,
        "effect_scope": scope,
        "affected_paths": list(paths),
    }


def test_completion_evidence_tracks_repair_and_verification_scope():
    unresolved = EvidenceLedger(
        effects=[effect("call_partial", "partial_success", "partial", ("x.py",))]
    )
    assert unresolved.assess_completion("").allowed is False

    repaired = EvidenceLedger(
        effects=[
            effect("call_partial", "partial_success", "partial", ("x.py",)),
            effect("call_repair", "ok", "changed", ("x.py",)),
        ]
    )
    assert repaired.assess_completion("").allowed is True

    verification = {
        "verification_id": "verify_workspace",
        "status": "passed",
        "freshness": "current",
        "workspace_fingerprint": "workspace-current",
    }
    workspace_partial = EvidenceLedger(
        effects=[effect("call_workspace", "partial_success", "partial", ("x.py",))],
        verifications=[verification],
    )
    assert workspace_partial.assess_completion("workspace-current").allowed is True

    memory_partial = EvidenceLedger(
        effects=[
            effect(
                "call_memory",
                "partial_success",
                "partial",
                (".pico/memory/MEMORY.md",),
                scope="project_memory",
            )
        ],
        verifications=[verification],
    )
    assert memory_partial.assess_completion("workspace-current").allowed is False

    memory_partial.effects.append(
        effect(
            "call_memory_repair",
            "ok",
            "changed",
            (".pico/memory/MEMORY.md",),
            scope="project_memory",
        )
    )
    assert memory_partial.assess_completion("workspace-current").allowed is True
