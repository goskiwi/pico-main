from pico.contracts import FailureInfo, ToolOutcome
from pico.progress import ProgressGovernor
from pico.recovery import RecoveryPolicy


def failed_outcome():
    return ToolOutcome(
        "call_failure",
        "run_shell",
        "error",
        "failed",
        "none",
        "exit_code: 1",
        "same-fingerprint",
        {"status": "admitted", "stages": []},
        failure=FailureInfo("tool_failed", "command", "same failure", True),
        workspace_fingerprint="workspace-a",
    )


def observed_outcome(call_fingerprint, content="same observation", workspace="workspace-a"):
    return ToolOutcome(
        "call_observation",
        "read_file",
        "ok",
        "completed",
        "none",
        content,
        call_fingerprint,
        {"status": "admitted", "stages": []},
        workspace_fingerprint=workspace,
    )


def test_progress_governor_replans_repeated_failure():
    governor = ProgressGovernor()

    first = governor.observe_tool(failed_outcome())
    second = governor.observe_tool(failed_outcome())

    assert first.decision == "repair"
    assert second.decision == "replan"
    assert second.strategy_revision == 1
    assert second.guidance()


def test_progress_state_replays_from_events():
    decision = ProgressGovernor().observe_tool(failed_outcome())
    events = [
        {
            "event_type": "progress_decided",
            "payload": decision.to_dict(),
        }
    ]

    restored = ProgressGovernor.from_events(events)
    assert restored.no_progress_steps == 1
    assert restored.failure_counts[decision.failure_signature] == 1


def test_equivalent_observations_do_not_manufacture_progress_with_changed_arguments():
    governor = ProgressGovernor()

    first = governor.observe_tool(observed_outcome("read-lines-1-20"))
    repeats = [
        governor.observe_tool(observed_outcome(f"read-lines-1-{end}"))
        for end in (40, 80)
    ]

    assert first.evidence_delta == 1
    assert [decision.evidence_delta for decision in repeats] == [0, 0]
    assert repeats[0].reason == "observation already seen"
    assert repeats[-1].decision == "replan"
    assert repeats[-1].reason == "equivalent observation repeated"
    assert repeats[-1].no_progress_steps == 2


def test_changed_observation_or_workspace_is_new_evidence():
    governor = ProgressGovernor()
    governor.observe_tool(observed_outcome("first"))

    changed_content = governor.observe_tool(
        observed_outcome("second", content="different observation")
    )
    changed_workspace = governor.observe_tool(
        observed_outcome("third", content="same observation", workspace="workspace-b")
    )

    assert changed_content.evidence_delta == 1
    assert changed_workspace.evidence_delta == 1


def test_restored_progress_recognizes_equivalent_observation_with_new_arguments():
    first = ProgressGovernor().observe_tool(observed_outcome("original-arguments"))
    restored = ProgressGovernor.from_events([
        {"event_type": "progress_decided", "payload": first.to_dict()}
    ])

    repeated = restored.observe_tool(observed_outcome("different-arguments"))

    assert repeated.evidence_delta == 0
    assert repeated.no_progress_steps == 1


def test_progress_does_not_request_code_verification_for_memory_commit():
    outcome = ToolOutcome(
        "call_memory",
        "memory_store",
        "ok",
        "completed",
        "changed",
        "created project memory project_release_command.md",
        "memory-fingerprint",
        {"status": "admitted", "stages": []},
        affected_paths=(".pico/memory/cards/project_release_command.md",),
        workspace_fingerprint="workspace-a",
        metadata={"effect_scope": "project_memory"},
    )

    decision = ProgressGovernor().observe_tool(outcome)

    assert decision.decision == "continue"
    assert decision.reason == "durable project memory changed"


def test_recovery_retry_occurrence_is_scoped_to_one_run():
    policy = RecoveryPolicy()
    failure = FailureInfo("tool_failed", "execution", "transient", True)

    first_run = policy.assess(failure, status="error", fingerprint="same", scope="run-a")
    repeated = policy.assess(failure, status="error", fingerprint="same", scope="run-a")
    new_run = policy.assess(failure, status="error", fingerprint="same", scope="run-b")

    assert first_run.action == "retry"
    assert repeated.action == "replan"
    assert new_run.action == "retry"
