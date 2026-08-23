from pico.contracts import ToolOutcome
from pico.evidence import RunEvidence
from pico.run_log import RunEvent


def changed_outcome():
    return ToolOutcome(
        tool_call_id="call_1",
        tool_name="edit_file",
        status="success",
        execution_state="completed",
        side_effect_state="changed",
        content="patched",
        affected_paths=("src/app.py",),
        effect_scope="workspace",
    )


def event(kind, payload, sequence=1):
    return RunEvent(
        event_id=f"run:event:{sequence:06d}",
        sequence=sequence,
        run_id="run",
        task_id="task",
        session_id="session",
        kind=kind,
        timestamp="2026-01-01T00:00:00+00:00",
        payload=payload,
    )


def test_live_and_run_log_recovery_build_identical_evidence():
    outcome = changed_outcome()
    run_event = event(
        "tool_result",
        {
            "tool_call_id": outcome.tool_call_id,
            "tool_name": outcome.tool_name,
            "workspace_revision": 1,
            "outcome": outcome.to_dict(),
        },
    )
    live = RunEvidence()
    live.apply_event(run_event)
    restored = RunEvidence.from_events([run_event])

    assert restored.effects == live.effects
    assert restored.verifications == live.verifications


def test_workspace_fact_invalidates_current_verification():
    evidence = RunEvidence()
    evidence.apply_event(
        event(
            "verification_result",
            {
                "status": "passed",
                "freshness": "current",
                "workspace_fingerprint": "workspace-before",
            },
        )
    )

    evidence.apply_event(
        event(
            "tool_result",
                {
                    "tool_call_id": "call_1",
                    "tool_name": "edit_file",
                    "workspace_revision": 1,
                    "outcome": changed_outcome().to_dict(),
            },
            sequence=2,
        )
    )

    assert evidence.verifications[0]["freshness"] == "stale"


def effect(call_id, status, side_effect_state, paths, scope="workspace"):
    return {
        "tool_call_id": call_id,
        "status": status,
        "side_effect_state": side_effect_state,
        "effect_scope": scope,
        "affected_paths": list(paths),
    }


def test_completion_evidence_tracks_repair_and_verification_scope():
    unresolved = RunEvidence(
        effects=[effect("call_partial", "partial_success", "partial", ("x.py",))]
    )
    assert unresolved.assess_completion("").allowed is False

    repaired = RunEvidence(
        effects=[
            effect("call_partial", "partial_success", "partial", ("x.py",)),
            effect("call_repair", "success", "changed", ("x.py",)),
        ]
    )
    assert repaired.assess_completion("").allowed is True

    verification = {
        "status": "passed",
        "freshness": "current",
        "workspace_fingerprint": "workspace-current",
    }
    workspace_partial = RunEvidence(
        effects=[effect("call_workspace", "partial_success", "partial", ("x.py",))],
        verifications=[verification],
    )
    assert workspace_partial.assess_completion("workspace-current").allowed is True

    memory_partial = RunEvidence(
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
            "success",
            "changed",
            (".pico/memory/MEMORY.md",),
            scope="project_memory",
        )
    )
    assert memory_partial.assess_completion("workspace-current").allowed is True
