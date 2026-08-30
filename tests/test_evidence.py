from pico.contracts import FailureInfo, ToolOutcome
from pico.evidence import RunEvidence, verification_is_current
from pico.run_log import RunEvent

READ_TASK = {
    "task_kind": "read_only",
    "requires_workspace_change": False,
    "requires_verification": False,
}
NO_CHANGE_TASK = {
    "task_kind": "modify",
    "requires_workspace_change": False,
    "requires_verification": False,
}
MODIFY_TASK = {
    "task_kind": "modify",
    "requires_workspace_change": True,
    "requires_verification": False,
}


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


def tool_event(
    sequence,
    *,
    call_id,
    status="success",
    side_effect_state="changed",
    before="sha256:a",
    after="sha256:b",
    path="src/app.py",
):
    failure = (
        None
        if status == "success"
        else FailureInfo("interrupted", "interrupted", "no_retry")
    )
    outcome = ToolOutcome(
        call_id,
        "edit_file",
        status,
        "completed" if status == "success" else "failed",
        side_effect_state,
        "patched",
        structured={
            "path_transitions": (
                [
                    {
                        "path": path,
                        "before_state": before,
                        "after_state": after,
                        "before_artifact_id": (
                            "" if before == "absent" else "preimage_synthetic"
                        ),
                    }
                ]
                if side_effect_state != "unknown"
                else []
            )
        },
        failure=failure,
        affected_paths=(path,) if side_effect_state != "unknown" else (),
        effect_scope="workspace",
    )
    return event(
        "tool_result",
        {
            "tool_call_id": call_id,
            "tool_name": "edit_file",
            "outcome": outcome.to_dict(),
        },
        sequence,
    )


def verification(sequence, path_state="sha256:b", status="passed"):
    return {
        "status": status,
        "started_workspace_mutation_sequence": sequence,
        "finished_workspace_mutation_sequence": sequence,
        "started_changed_path_states": {"src/app.py": path_state},
        "finished_changed_path_states": {"src/app.py": path_state},
    }


def test_live_and_replay_build_same_small_evidence():
    result = tool_event(1, call_id="edit")
    live = RunEvidence().apply_event(result)
    replayed = RunEvidence.from_events([result])

    assert replayed.to_dict() == live.to_dict()
    assert replayed.changed_paths == ["src/app.py"]
    assert replayed.last_workspace_mutation_sequence == 1


def test_observation_count_only_counts_successful_observations():
    success = ToolOutcome(
        "read",
        "read_file",
        "success",
        "completed",
        "none",
        "read",
    )
    evidence = RunEvidence.from_events(
        [
            event(
                "tool_result",
                {
                    "tool_call_id": "read",
                    "tool_name": "read_file",
                    "outcome": success.to_dict(),
                },
            )
        ]
    )
    assert evidence.successful_observation_count == 1


def test_a_to_b_to_a_is_touched_but_not_net_changed():
    evidence = RunEvidence.from_events(
        [
            tool_event(1, call_id="ab", before="a", after="b"),
            tool_event(2, call_id="ba", before="b", after="a"),
        ]
    )
    assert evidence.touched_paths == ["src/app.py"]
    assert evidence.changed_paths == []
    assert evidence.has_net_workspace_change is False


def test_verification_freshness_is_derived_not_persisted():
    record = verification(4)
    assert "freshness" not in record
    assert verification_is_current(record, 4, {"src/app.py": "sha256:b"})
    assert not verification_is_current(record, 5, {"src/app.py": "sha256:b"})


def test_partial_requires_repair_and_current_passing_verification():
    evidence = RunEvidence.from_events(
        [
            tool_event(
                1,
                call_id="partial",
                status="partial_success",
                side_effect_state="partial",
                before="a",
                after="partial",
            ),
            tool_event(2, call_id="repair", before="partial", after="fixed"),
        ]
    )
    assert not evidence.assess_completion().allowed
    evidence.verifications.append(verification(2, "fixed"))
    assert evidence.assess_completion(2, {"src/app.py": "fixed"}).allowed


def test_repaired_project_memory_partial_does_not_require_workspace_verification():
    path = ".pico/memory/cards/decision.md"
    partial = ToolOutcome(
        "memory_partial",
        "memory_store",
        "partial_success",
        "failed",
        "partial",
        "memory write was interrupted",
        failure=FailureInfo("interrupted", "interrupted", "no_retry"),
        affected_paths=(path,),
        effect_scope="project_memory",
    )
    repair = ToolOutcome(
        "memory_repair",
        "memory_store",
        "success",
        "completed",
        "changed",
        "memory card replaced",
        affected_paths=(path,),
        effect_scope="project_memory",
    )
    evidence = RunEvidence.from_events(
        [
            event(
                "tool_result",
                {
                    "tool_call_id": partial.tool_call_id,
                    "tool_name": partial.tool_name,
                    "outcome": partial.to_dict(),
                },
                1,
            ),
            event(
                "tool_result",
                {
                    "tool_call_id": repair.tool_call_id,
                    "tool_name": repair.tool_name,
                    "outcome": repair.to_dict(),
                },
                2,
            ),
        ]
    )

    assert evidence.repaired_partials_requiring_verification() == []
    assert evidence.unresolved_effects() == []
    assert evidence.assess_completion().allowed


def test_unknown_effect_is_never_cleared_by_verification():
    evidence = RunEvidence.from_events(
        [
            tool_event(
                1,
                call_id="unknown",
                status="partial_success",
                side_effect_state="unknown",
            )
        ]
    )
    evidence.verifications.append(verification(1))
    assert not evidence.assess_completion(
        1,
        {"src/app.py": "sha256:b"},
    ).allowed
