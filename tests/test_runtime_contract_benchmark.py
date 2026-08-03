import json
from pathlib import Path

import pytest

from pico.evaluation import runtime_contract_benchmark as contracts


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_PATH = PROJECT_ROOT / "benchmarks" / "runtime_contract_tasks_v1.json"


def test_runtime_contract_manifest_has_the_four_frozen_families():
    benchmark = contracts.load_runtime_contract_benchmark(
        BENCHMARK_PATH,
        PROJECT_ROOT,
    )

    assert [task["id"] for task in benchmark["tasks"]] == [
        "ctx_budget_preserves_current_request",
        "memory_deduplicates_unchanged_read",
        "resume_validates_checkpoint_freshness",
        "tool_classifies_mutating_failure",
    ]
    assert {task["family"] for task in benchmark["tasks"]} == {
        "context_management",
        "working_memory",
        "checkpoint_resume",
        "tool_governance",
    }


def test_runtime_contract_runner_writes_paired_machine_readable_evidence(tmp_path):
    artifact_path = tmp_path / "runtime-contract.json"
    report_path = tmp_path / "runtime-contract.md"
    runner = contracts.RuntimeContractBenchmarkRunner(
        benchmark_path=BENCHMARK_PATH,
        artifact_path=artifact_path,
        report_path=report_path,
        workspace_root=tmp_path / "workspaces",
        repetitions=1,
    )

    artifact = runner.run()

    assert artifact["execution_mode"] == "deterministic_scripted"
    assert artifact["no_remote_model_calls"] is True
    assert artifact["summary"]["passed"] == 4
    assert artifact["summary"]["attempt_count"] == 4
    assert all(row["passed"] for row in artifact["rows"])
    assert all(row["verifier"]["checks"] for row in artifact["rows"])
    assert all(row["outcome_fingerprint"].startswith("sha256:") for row in artifact["rows"])
    assert all(
        metrics["unique_outcome_fingerprints"] == 1
        for metrics in artifact["summary"]["tasks"].values()
    )

    memory = next(
        row
        for row in artifact["rows"]
        if row["task_id"] == "memory_deduplicates_unchanged_read"
    )
    assert memory["control"]["metrics"]["physical_read_calls"] == 3
    assert memory["candidate"]["metrics"]["physical_read_calls"] == 2

    persisted = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert persisted["benchmark"]["task_ids"] == [
        "ctx_budget_preserves_current_request",
        "memory_deduplicates_unchanged_read",
        "resume_validates_checkpoint_freshness",
        "tool_classifies_mutating_failure",
    ]
    report = report_path.read_text(encoding="utf-8")
    assert "Remote model calls: **0**" in report
    assert "Paired observations" in report


def test_runtime_contract_runner_requires_a_clean_worktree_when_requested(
    tmp_path,
    monkeypatch,
):
    runner = contracts.RuntimeContractBenchmarkRunner(
        benchmark_path=BENCHMARK_PATH,
        artifact_path=tmp_path / "artifact.json",
        report_path=tmp_path / "report.md",
        workspace_root=tmp_path / "workspaces",
        repetitions=1,
        require_clean_worktree=True,
    )
    monkeypatch.setattr(contracts, "git_value", lambda *args, **kwargs: " M file.py")

    with pytest.raises(RuntimeError, match="requires a clean worktree"):
        runner.run()
