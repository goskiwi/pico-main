import json
from pathlib import Path

import pytest

from pico.evaluation.evaluator import (
    _portable_path,
    load_benchmark,
    run_harness_regression_v3,
    summarize_rows,
)


def test_portable_path_removes_local_checkout_prefix():
    path = Path.cwd() / ".pico" / "evaluation" / "fixture"

    assert _portable_path(path) == ".pico/evaluation/fixture"


def test_native_benchmark_schema_and_no_legacy_protocol():
    benchmark = load_benchmark(Path("benchmarks/coding_tasks.json"))
    assert benchmark["schema_version"] == 2
    assert len(benchmark["tasks"]) == 5
    assert all("expected_revision" not in task for task in benchmark["tasks"])
    assert "<tool" not in json.dumps(benchmark)


def test_benchmark_rejects_missing_fields(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"schema_version":2,"description":"x","tasks":[{"id":"bad"}]}')
    with pytest.raises(ValueError, match="required"):
        load_benchmark(path)


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


def test_summarize_rows_reports_failures():
    summary = summarize_rows([
        {"passed": True, "within_budget": True, "verifier_passed": True},
        {"passed": False, "within_budget": False, "verifier_passed": False,
         "failure_category": "verifier_failed"},
    ])
    assert summary["pass_rate"] == 0.5
    assert summary["failure_category_counts"] == {"verifier_failed": 1}
