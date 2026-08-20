from pathlib import Path

from pico.evaluation.evaluator import run_harness_regression_v3


def test_native_harness_runs_fresh_fixtures_and_recovery(tmp_path):
    artifact = run_harness_regression_v3(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=tmp_path / "harness.json",
        workspace_root=tmp_path / "workspaces",
    )
    assert artifact["artifact_type"] == "harness-regression-v3"
    assert artifact["summary"]["total_tasks"] == 5
    assert artifact["summary"]["pass_rate"] == 1.0
    assert artifact["summary"]["verifier_pass_rate"] == 1.0
    assert all(Path(row["run_dir"]).is_dir() for row in artifact["rows"])
    assert all(
        Path(row["run_dir"]).is_relative_to(Path(row["fixture_copy"]))
        for row in artifact["rows"]
    )
    assert {row["category"] for row in artifact["rows"]} >= {"edit", "recovery", "safety", "governance"}
