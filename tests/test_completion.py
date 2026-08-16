from pico.completion import CompletionGate
from pico.contracts import FailureInfo, ToolOutcome


def test_completion_gate_blocks_unresolved_partial_side_effects():
    gate = CompletionGate()
    gate.observe(
        ToolOutcome(
            "call_partial",
            "run_shell",
            "partial_success",
            "failed",
            "partial",
            "exit_code: 1",
            "fingerprint",
            {"status": "admitted", "stages": []},
            failure=FailureInfo("tool_partial_success", "command", retryable=True),
            affected_paths=("README.md",),
        )
    )

    decision = gate.assess()

    assert decision.allowed is False
    assert decision.status == "partial"
    assert "README.md" in decision.reason


def test_fresh_runtime_verification_can_reconcile_interrupted_effect():
    gate = CompletionGate()
    gate.restore_partial_paths(["README.md"])
    assert gate.assess().allowed is False
    gate.observe_verification(True)
    assert gate.assess().allowed is True
