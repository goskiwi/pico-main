from pathlib import Path

from evals.evaluator import run_harness_regression


def test_native_harness_runs_fresh_fixtures_and_recovery(tmp_path):
    artifact = run_harness_regression(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=tmp_path / "harness.json",
        workspace_root=tmp_path / "workspaces",
    )
    assert artifact["artifact_type"] == "harness-regression"
    assert artifact["summary"]["total_tasks"] == 5
    assert artifact["summary"]["pass_rate"] == 1.0
    assert artifact["summary"]["verifier_pass_rate"] == 1.0
    assert all(
        len(list((Path(row["fixture_copy"]) / ".pico" / "runs").iterdir())) == 1
        for row in artifact["rows"]
    )
    assert {row["category"] for row in artifact["rows"]} >= {"edit", "recovery", "safety", "governance"}

    repeated = run_harness_regression(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=tmp_path / "harness.json",
        workspace_root=tmp_path / "workspaces",
    )
    assert repeated == artifact
