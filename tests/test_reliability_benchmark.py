import os
from pathlib import Path

import pytest

from pico.evaluation.reliability_benchmark import (
    DEFAULT_RELIABILITY_BENCHMARK_PATH,
    ReliabilityBenchmarkRunner,
    load_reliability_benchmark,
)
from tests.fakes import FakeModelClient, final_action, tool_action_json
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


@pytest.mark.parametrize(
    ("task_id", "responses", "expected_dirty"),
    [
        (
            "undo_rejected_multifile_change",
            [
                tool_action_json(
                    '{"name":"patch_file","args":{"path":"checkout/pricing.py",'
                    '"old_text":"return amount * 2","new_text":"return amount * 3"}}'
                ),
                tool_action_json(
                    '{"name":"patch_file","args":{"path":"checkout/service.py",'
                    '"old_text":"\\"label\\": \\"standard\\"","new_text":'
                    '"\\"label\\": \\"experimental\\""}}'
                ),
                final_action("Applied both experimental changes."),
            ],
            False,
        ),
        (
            "undo_preserves_preexisting_dirty_file",
            [
                tool_action_json(
                    '{"name":"patch_file","args":{"path":"README.md",'
                    '"old_text":"# Checkout App","new_text":"# Experimental Checkout"}}'
                ),
                tool_action_json(
                    '{"name":"patch_file","args":{"path":"checkout/pricing.py",'
                    '"old_text":"return amount * 2","new_text":"return amount * 4"}}'
                ),
                final_action("Applied both experimental changes."),
            ],
            True,
        ),
    ],
)
def test_reliability_runner_proves_exact_undo_recovery(
    tmp_path,
    task_id,
    responses,
    expected_dirty,
):
    benchmark = load_reliability_benchmark(BENCHMARK_PATH, PROJECT_ROOT)
    task = next(item for item in benchmark["tasks"] if item["id"] == task_id)
    runner = ReliabilityBenchmarkRunner(
        benchmark_path=BENCHMARK_PATH,
        artifact_path=tmp_path / "artifact.json",
        report_path=tmp_path / "report.md",
        workspace_root=tmp_path / "workspaces",
    )
    runner._shared_model_client = FakeModelClient(responses)
    runner._sandbox = lambda workspace_root: VerifierTestSandbox(workspace_root)

    row = runner.run_task(task, repetition=1)

    assert row["passed"] is True
    assert row["pre_undo_verifier"]["exit_code"] != 0
    assert row["post_undo_verifier"]["exit_code"] == 0
    assert row["recovery"]["exact_restoration"] is True
    assert row["recovery"]["dirty_preserved"] is True
    assert bool(row["dirty_paths"]) is expected_dirty
    assert row["workspace_digests"]["pre_run"] == row["workspace_digests"]["after_undo"]
