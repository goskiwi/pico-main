import json
import os
from pathlib import Path

import pytest

from evaluation.reliability_benchmark import (
    DEFAULT_RELIABILITY_BENCHMARK_PATH,
    ReliabilityBenchmarkRunner,
    _snapshot_digest,
    _workspace_hashes,
    load_reliability_benchmark,
    render_reliability_markdown,
    summarize_reliability_rows,
    validate_reliability_benchmark,
)
from pico.models import FakeModelClient
from tests.helpers import UnitTestSandbox


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_PATH = PROJECT_ROOT / DEFAULT_RELIABILITY_BENCHMARK_PATH


class VerifierTestSandbox(UnitTestSandbox):
    def run(self, command, *, cwd, timeout, env=None):
        return super().run(
            command,
            cwd=cwd,
            timeout=timeout,
            env={**os.environ, **(env or {})},
        )


def _row(**updates):
    row = {
        "task_id": "undo-task",
        "category": "undo_recovery",
        "mode": "undo_recovery",
        "repetition": 1,
        "passed": True,
        "tool_steps": 2,
        "model_calls": 3,
        "model_failures": 0,
        "model_action_rejections": 0,
        "input_tokens": 100,
        "output_tokens": 20,
        "total_duration_ms": 500,
        "mutation_paths": ["checkout/pricing.py"],
        "dirty_paths": [],
        "repo_map_files": ["checkout/pricing.py"],
        "trace_parse_errors": [],
        "workspace_isolation": {"ok": True},
        "pre_undo_verifier": {"exit_code": 1},
        "recovery": {
            "passed": True,
            "exact_restoration": True,
            "dirty_preserved": True,
        },
    }
    row.update(updates)
    return row


def test_reliability_protocol_freezes_three_distinct_scenarios():
    benchmark = load_reliability_benchmark(
        BENCHMARK_PATH,
        PROJECT_ROOT,
    )

    assert benchmark["name"] == "pico-repo-map-undo-reliability-v1"
    assert [task["mode"] for task in benchmark["tasks"]] == [
        "task_success",
        "undo_recovery",
        "undo_recovery",
    ]
    dirty = benchmark["tasks"][2]
    assert dirty["dirty_paths"] == ["README.md"]
    assert dirty["preexisting_edits"][0]["path"] == "README.md"


def test_reliability_protocol_rejects_dirty_path_without_preexisting_edit():
    payload = json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))
    payload["tasks"][2]["preexisting_edits"] = []

    with pytest.raises(
        ValueError,
        match="dirty_paths must have preexisting edits",
    ):
        validate_reliability_benchmark(payload, PROJECT_ROOT)


def test_workspace_hashes_ignore_runtime_artifacts(tmp_path):
    (tmp_path / "source.py").write_text("value = 1\n", encoding="utf-8")
    runtime = tmp_path / ".pico" / "runs"
    runtime.mkdir(parents=True)
    (runtime / "trace.jsonl").write_text("{}\n", encoding="utf-8")

    before = _workspace_hashes(tmp_path)
    (runtime / "trace.jsonl").write_text('{"changed": true}\n', encoding="utf-8")
    after = _workspace_hashes(tmp_path)

    assert before == after
    assert _snapshot_digest(before) == _snapshot_digest(after)


def test_summary_and_markdown_report_recovery_and_dirty_preservation():
    rows = [
        _row(),
        _row(
            task_id="dirty-task",
            category="undo_dirty_workspace",
            dirty_paths=["README.md"],
        ),
        _row(
            task_id="repo-map-task",
            category="repo_map_task_success",
            mode="task_success",
            pre_undo_verifier={"exit_code": 0},
            recovery={
                "passed": False,
                "exact_restoration": False,
                "dirty_preserved": True,
            },
        ),
    ]
    summary = summarize_reliability_rows(rows)
    artifact = {
        "captured_at": "2026-07-24T00:00:00Z",
        "model": "test-model",
        "repetitions": 1,
        "runtime": {
            "commit_sha": "abc123",
            "branch": "test",
            "working_tree_dirty": False,
        },
        "benchmark": {
            "fixture_snapshot_id": "sha256:fixture",
            "evaluation_snapshot_id": "sha256:evaluation",
        },
        "summary": summary,
        "rows": rows,
    }

    report = render_reliability_markdown(artifact)

    assert summary["passed"] == 3
    assert summary["recovered"] == 2
    assert summary["recovery_rate"] == 1.0
    assert summary["dirty_preservation_rate"] == 1.0
    assert summary["model_failures"] == 0
    assert summary["workspace_isolation_failures"] == 0
    assert "Undo recovery: **2/2 (100.0%)**" in report
    assert "0 / 0 / 0 / 0" in report
    assert "`dirty-task`" in report


@pytest.mark.parametrize(
    ("task_id", "responses", "expected_dirty"),
    [
        (
            "undo_rejected_multifile_change",
            [
                (
                    '<tool>{"name":"patch_file","args":{"path":'
                    '"checkout/pricing.py","old_text":"return amount * 2",'
                    '"new_text":"return amount * 3"}}</tool>'
                ),
                (
                    '<tool>{"name":"patch_file","args":{"path":'
                    '"checkout/service.py","old_text":'
                    '"\\"label\\": \\"standard\\"","new_text":'
                    '"\\"label\\": \\"experimental\\""}}</tool>'
                ),
                "<final>Applied both experimental changes.</final>",
            ],
            False,
        ),
        (
            "undo_preserves_preexisting_dirty_file",
            [
                (
                    '<tool>{"name":"patch_file","args":{"path":'
                    '"README.md","old_text":"# Checkout App",'
                    '"new_text":"# Experimental Checkout"}}</tool>'
                ),
                (
                    '<tool>{"name":"patch_file","args":{"path":'
                    '"checkout/pricing.py","old_text":"return amount * 2",'
                    '"new_text":"return amount * 4"}}</tool>'
                ),
                "<final>Applied both experimental changes.</final>",
            ],
            True,
        ),
    ],
)
def test_runner_proves_exact_undo_recovery(
    tmp_path,
    task_id,
    responses,
    expected_dirty,
):
    benchmark = load_reliability_benchmark(
        BENCHMARK_PATH,
        PROJECT_ROOT,
    )
    task = next(
        item for item in benchmark["tasks"] if item["id"] == task_id
    )
    runner = ReliabilityBenchmarkRunner(
        benchmark_path=BENCHMARK_PATH,
        artifact_path=tmp_path / "artifact.json",
        report_path=tmp_path / "report.md",
        workspace_root=tmp_path / "workspaces",
    )
    runner._shared_model_client = FakeModelClient(responses)
    runner._sandbox = lambda workspace_root: VerifierTestSandbox(
        workspace_root
    )

    row = runner.run_task(task, repetition=1)

    assert row["passed"] is True
    assert row["pre_undo_verifier"]["exit_code"] != 0
    assert row["post_undo_verifier"]["exit_code"] == 0
    assert row["missing_expected_changes"] == []
    assert row["recovery"]["exact_restoration"] is True
    assert row["recovery"]["dirty_preserved"] is True
    assert bool(row["dirty_paths"]) is expected_dirty
    assert (
        row["workspace_digests"]["pre_run"]
        == row["workspace_digests"]["after_undo"]
    )
