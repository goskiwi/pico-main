import json
import os
from collections import deque
from pathlib import Path

import pytest

from pico.evaluation.continuation_benchmark import (
    LiveContinuationBenchmarkRunner,
    load_continuation_benchmark,
)
from tests.fakes import FakeModelClient, final_action, tool_action_json
from tests.helpers import UnitTestSandbox


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_PATH = PROJECT_ROOT / "benchmarks" / "live_continuation_tasks_v1.json"


class VerifierTestSandbox(UnitTestSandbox):
    def run(self, command, *, cwd, timeout, env=None):
        return super().run(
            command,
            cwd=cwd,
            timeout=timeout,
            env={**os.environ, **(env or {})},
        )


def _tool(name, args):
    return tool_action_json(json.dumps({"name": name, "args": args}))


def _runner(tmp_path, clients):
    runner = LiveContinuationBenchmarkRunner(
        benchmark_path=BENCHMARK_PATH,
        artifact_path=tmp_path / "artifact.json",
        report_path=tmp_path / "report.md",
        workspace_root=tmp_path / "workspaces",
        model_client_factory=clients.popleft,
    )
    runner._sandbox = lambda workspace_root: VerifierTestSandbox(workspace_root)
    return runner


def _memory_case(task_id):
    benchmark = load_continuation_benchmark(BENCHMARK_PATH, PROJECT_ROOT)
    return next(task for task in benchmark["memory_cases"] if task["id"] == task_id)


def _resume_case(task_id):
    benchmark = load_continuation_benchmark(BENCHMARK_PATH, PROJECT_ROOT)
    return next(task for task in benchmark["resume_cases"] if task["id"] == task_id)


def test_manifest_freezes_valid_tasks_and_history_free_phase_two_prompts():
    benchmark = load_continuation_benchmark(BENCHMARK_PATH, PROJECT_ROOT)

    assert len(benchmark["memory_cases"]) == 4
    assert len(benchmark["resume_cases"]) == 7
    for task in [*benchmark["memory_cases"], *benchmark["resume_cases"]]:
        lower = task["phase_two_prompt"].lower()
        assert not any(
            signal in lower
            for signal in ("continue", "resume", "previous", "earlier", "上次", "之前")
        )
        assert (PROJECT_ROOT / task["fixture_repo"] / task["source_path"]).is_file()


def test_memory_control_changes_only_memory_and_records_phase_two_reads(tmp_path):
    task = _memory_case("memory_env_assignment")
    candidate_clients = deque(
        [
            FakeModelClient(
                [
                    _tool("read_file", {"files": [{"path": "facts/release.env"}]}),
                    final_action("ACK"),
                ]
            ),
            FakeModelClient(
                [
                    _tool(
                        "write_file",
                        {
                            "path": "followup-result.txt",
                            "content": "amber-memory-71\n",
                        },
                    ),
                    final_action("done"),
                ]
            ),
        ]
    )
    control_clients = deque(
        [
            FakeModelClient(
                [
                    _tool("read_file", {"files": [{"path": "facts/release.env"}]}),
                    final_action("ACK"),
                ]
            ),
            FakeModelClient(
                [
                    _tool("read_file", {"files": [{"path": "facts/release.env"}]}),
                    _tool(
                        "write_file",
                        {
                            "path": "followup-result.txt",
                            "content": "amber-memory-71\n",
                        },
                    ),
                    final_action("done"),
                ]
            ),
        ]
    )

    candidate = _runner(tmp_path / "candidate", candidate_clients).run_memory_case(
        task, variant="working_memory", repetition=1
    )
    control = _runner(tmp_path / "control", control_clients).run_memory_case(
        task, variant="memory_disabled", repetition=1
    )

    assert candidate["passed"] is True
    assert control["passed"] is True
    assert candidate["phase_two"]["memory_summary_present_before_phase"] is True
    assert control["phase_two"]["memory_summary_present_before_phase"] is False
    assert candidate["followup_source_read"]["successful_physical_file_accesses"] == 0
    assert control["followup_source_read"]["successful_physical_file_accesses"] == 1
    assert candidate["phase_two"]["recent_run_selection_count"] == 0
    assert control["phase_two"]["recent_run_selection_count"] == 0


@pytest.mark.parametrize(
    ("task_id", "phase_two_actions"),
    [
        (
            "resume_clean_checkpoint",
            [
                _tool(
                    "write_file",
                    {
                        "path": "recovery-result.txt",
                        "content": "resume-clean-11\n",
                    },
                ),
                final_action("done"),
            ],
        ),
        (
            "resume_key_file_replaced",
            [
                _tool("read_file", {"files": [{"path": "state/contract.txt"}]}),
                _tool(
                    "write_file",
                    {
                        "path": "recovery-result.txt",
                        "content": "resume-replaced-65\n",
                    },
                ),
                final_action("done"),
            ],
        ),
    ],
)
def test_resume_runner_rebuilds_from_session_and_validates_status(
    tmp_path, task_id, phase_two_actions
):
    task = _resume_case(task_id)
    clients = deque(
        [
            FakeModelClient(
                [_tool("read_file", {"files": [{"path": "state/contract.txt"}]})]
            ),
            FakeModelClient(phase_two_actions),
        ]
    )

    row = _runner(tmp_path, clients).run_resume_case(task, repetition=1)

    assert row["passed"] is True
    assert row["phase_one"]["status"] == "failed"
    assert row["phase_one"]["stop_reason"] == "model_error"
    assert row["phase_two"]["resume_status"]["status"] == task[
        "expected_resume_status"
    ]
    assert row["phase_two"]["recent_run_selection_count"] == 0
    assert row["totals"]["expected_injected_model_failures"] == 1
    assert row["totals"]["unexpected_model_failures"] == 0


def test_hidden_verifier_rejects_wrong_output_before_accepting_exact_output(tmp_path):
    runner = _runner(tmp_path, deque())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "followup-result.txt").write_text("wrong\n", encoding="utf-8")

    failed, _, skipped = runner._verify(
        workspace,
        expected_output="expected",
        output_path="followup-result.txt",
        enabled=True,
    )
    assert skipped is False
    assert failed.returncode != 0

    (workspace / "followup-result.txt").write_text("expected\n", encoding="utf-8")
    passed, _, skipped = runner._verify(
        workspace,
        expected_output="expected",
        output_path="followup-result.txt",
        enabled=True,
    )
    assert skipped is False
    assert passed.returncode == 0
