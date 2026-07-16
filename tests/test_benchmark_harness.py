"""Offline unit tests for the live-LLM benchmark harness; no model API is called."""

import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from pico.models import FakeModelClient
from evaluation.real_benchmark import (
    RealWorldBenchmarkRunner,
    compare_real_benchmark_artifacts,
    load_real_benchmark,
    render_real_benchmark_comparison_markdown,
    render_real_benchmark_markdown,
    summarize_real_rows,
)
from pico.sandbox import SandboxResult
from tests.helpers import UnitTestSandbox


ROOT = Path(__file__).resolve().parent.parent


class BenchmarkTestSandbox(UnitTestSandbox):
    def run(self, command, *, cwd, timeout, env=None):
        process_env = os.environ.copy()
        process_env.update(env or {})
        completed = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=process_env,
        )
        return SandboxResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


def test_live_benchmark_manifest_has_ten_independent_engineering_tasks():
    benchmark = load_real_benchmark(ROOT / "benchmarks" / "real_world_tasks.json")

    assert len(benchmark["tasks"]) == 10
    assert len({task["fixture_repo"] for task in benchmark["tasks"]}) == 10
    assert Counter(task["category"] for task in benchmark["tasks"]) == {
        "bugfix": 4,
        "feature": 2,
        "test-addition": 2,
        "documentation": 1,
        "refactor": 1,
    }
    assert all(task["verifier_files"] for task in benchmark["tasks"])
    assert all("run_shell" in task["allowed_tools"] for task in benchmark["tasks"])


def test_hidden_verifiers_reject_every_unmodified_fixture(tmp_path):
    benchmark = load_real_benchmark(ROOT / "benchmarks" / "real_world_tasks.json")

    for task in benchmark["tasks"]:
        workspace = tmp_path / task["id"]
        shutil.copytree(ROOT / task["fixture_repo"], workspace)
        for verifier_file in task["verifier_files"]:
            target = workspace / verifier_file["target"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / verifier_file["source"], target)
        command = task["verifier_command"].replace(
            "pytest", f'"{sys.executable}" -m pytest', 1
        )
        completed = subprocess.run(
            command,
            cwd=workspace,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode != 0, (
            f"{task['id']} hidden verifier accepted the untouched fixture:\n"
            f"{completed.stdout}\n{completed.stderr}"
        )


def test_v2_has_five_independent_heldout_tasks_and_rejects_untouched_fixtures(tmp_path):
    benchmark = load_real_benchmark(ROOT / "benchmarks" / "real_world_tasks_v2.json")

    assert len(benchmark["tasks"]) == 5
    assert len({task["fixture_repo"] for task in benchmark["tasks"]}) == 5
    assert Counter(task["category"] for task in benchmark["tasks"]) == {
        "bugfix": 2,
        "feature": 1,
        "test-addition": 1,
        "refactor": 1,
    }
    for task in benchmark["tasks"]:
        workspace = tmp_path / task["id"]
        shutil.copytree(ROOT / task["fixture_repo"], workspace)
        for verifier_file in task["verifier_files"]:
            target = workspace / verifier_file["target"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / verifier_file["source"], target)
        command = task["verifier_command"].replace(
            "pytest", f'"{sys.executable}" -m pytest', 1
        )
        completed = subprocess.run(
            command,
            cwd=workspace,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode != 0, f"{task['id']} accepted the untouched fixture"


def test_harness_uses_fresh_copy_injects_hidden_verifier_and_cleans_it_up(tmp_path):
    outputs = [
        (
            '<tool name="patch_file" path="inventory.py">'
            "<old_text>    return cleaned\n</old_text>"
            "<new_text>    return cleaned.upper()\n</new_text>"
            "</tool>"
        ),
        "<final>Implemented SKU normalization and preserved validation.</final>",
    ]
    runner = RealWorldBenchmarkRunner(
        benchmark_path=ROOT / "benchmarks" / "real_world_tasks.json",
        artifact_path=tmp_path / "artifacts" / "real-world-benchmark-v1.json",
        report_path=tmp_path / "docs" / "real-world-benchmark-v1.md",
        workspace_root=tmp_path / "workspaces",
        model_client_factory=lambda **_kwargs: FakeModelClient(outputs),
        sandbox_factory=BenchmarkTestSandbox,
    )

    artifact = runner.run(task_ids=["inventory_normalize_sku"])

    assert artifact["execution_mode"] == "offline_harness_test"
    row = artifact["rows"][0]
    workspace = tmp_path / "workspaces" / row["workspace"]
    assert row["passed"] is True
    assert row["failure_category"] == ""
    assert row["model_calls"] == 2
    assert row["tool_steps"] == 1
    assert row["verifier"]["exit_code"] == 0
    assert row["changed_files"] == ["inventory.py"]
    assert not (workspace / ".benchmark_hidden").exists()
    assert "return cleaned.upper()" in (workspace / "inventory.py").read_text(
        encoding="utf-8"
    )
    assert (
        ROOT / "benchmarks" / "fixtures" / "real" / "inventory_normalize" / "inventory.py"
    ).read_text(encoding="utf-8").endswith("    return cleaned\n")
    assert runner.artifact_path.exists()
    assert runner.report_path.exists()


def test_summary_and_markdown_show_variant_delta_and_failure_categories():
    rows = [
        {
            "task_id": "one",
            "category": "bugfix",
            "variant": "full",
            "passed": True,
            "failure_category": "",
            "tool_steps": 2,
            "model_calls": 3,
            "input_tokens": 100,
            "output_tokens": 20,
            "cached_tokens": 10,
            "agent_duration_ms": 800,
            "total_duration_ms": 1000,
        },
        {
            "task_id": "one",
            "category": "bugfix",
            "variant": "no_memory_context",
            "passed": False,
            "failure_category": "verifier_failed",
            "tool_steps": 4,
            "model_calls": 5,
            "input_tokens": 120,
            "output_tokens": 30,
            "cached_tokens": 0,
            "agent_duration_ms": 1800,
            "total_duration_ms": 2000,
        },
    ]
    summary = summarize_real_rows(rows)
    artifact = {
        "captured_at": "2026-07-14T00:00:00Z",
        "provider": "openai",
        "model": "example-model",
        "runtime": {"commit_sha": "abc123", "branch": "main"},
        "benchmark": {"task_count": 1, "fixture_snapshot_id": "sha256:test"},
        "repetitions": 1,
        "sandbox": {
            "image": "pico-sandbox:latest",
            "cpus": 4.0,
            "memory": "4g",
            "pids_limit": 512,
        },
        "summary": summary,
        "rows": rows,
    }

    report = render_real_benchmark_markdown(artifact)

    assert summary["variants"]["full"]["pass_rate"] == 1.0
    assert summary["variants"]["no_memory_context"]["pass_rate"] == 0.0
    assert summary["comparison"]["pass_rate_delta"] == 1.0
    assert summary["failure_category_counts"] == {"verifier_failed": 1}
    assert "Pass-rate delta (full - no_memory_context): +100.0%" in report
    assert "verifier_failed" in report


def test_comparison_requires_matched_snapshot_and_reports_protocol_delta():
    def artifact(*, passed, calls, rejected=0, snapshot="sha256:same"):
        return {
            "model": "example-model",
            "benchmark": {"fixture_snapshot_id": snapshot},
            "rows": [
                {
                    "task_id": "one",
                    "variant": "full",
                    "repetition": 1,
                    "passed": passed,
                    "model_calls": calls,
                    "model_action_rejections": rejected,
                }
            ],
        }

    comparison = compare_real_benchmark_artifacts(
        artifact(passed=False, calls=9, rejected=2),
        artifact(passed=True, calls=4),
    )
    report = render_real_benchmark_comparison_markdown(comparison)

    assert comparison["summary"]["pass_rate_delta"] == 1.0
    assert comparison["summary"]["avg_model_calls_delta"] == -5.0
    assert "100.0%" in report
    assert "9" in report and "4" in report

    with pytest.raises(ValueError, match="snapshots do not match"):
        compare_real_benchmark_artifacts(
            artifact(passed=False, calls=9),
            artifact(passed=True, calls=4, snapshot="sha256:different"),
        )
