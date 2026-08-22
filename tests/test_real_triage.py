import subprocess
from pathlib import Path
from types import SimpleNamespace

from scripts.materialize_real_oss import load_manifest as load_real_manifest
from scripts.run_official_public_tests import load_manifest as load_official_manifest
from scripts.run_real_triage import (
    prepare_triage_workspace,
    summarize_runs,
    usage_metrics,
    visible_command,
)

TASK_ID = "click_empty_bytes_echo"


def test_real_triage_workspace_is_a_clean_failing_baseline(tmp_path):
    real = load_real_manifest(Path("validation/real_oss_suite.json"))
    real_task = next(task for task in real["tasks"] if task["id"] == TASK_ID)
    official = load_official_manifest(Path("validation/official_public_tests.json"))
    official_task = next(task for task in official["tasks"] if task["id"] == TASK_ID)

    baseline = prepare_triage_workspace(tmp_path / "click", real_task, official_task)

    assert len(baseline) == 40
    assert "BytesIO" in (tmp_path / "click" / "tests" / "test_utils.py").read_text(
        encoding="utf-8"
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tmp_path / "click",
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert status == ""
    command = visible_command(official_task)
    assert command.startswith("PYTHONPATH=src python -m pytest")
    assert "--tb=short" in command
    assert official_task["official_test_nodes"][0] in command


def test_real_triage_baseline_tracks_ignored_generated_source(tmp_path):
    real = load_real_manifest(Path("validation/real_oss_suite.json"))
    real_task = next(task for task in real["tasks"] if task["id"] == "urllib3_port_zero")
    official = load_official_manifest(Path("validation/official_public_tests.json"))
    official_task = next(
        task for task in official["tasks"] if task["id"] == "urllib3_port_zero"
    )

    workspace = tmp_path / "urllib3"
    prepare_triage_workspace(workspace, real_task, official_task)

    tracked = subprocess.run(
        ["git", "ls-files", "src/urllib3/_version.py"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert tracked == "src/urllib3/_version.py"


def test_usage_metrics_separate_gross_cached_and_uncached_input():
    turns = [
        SimpleNamespace(
            payload={
                "completion_metadata": {
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "cached_tokens": 6,
                }
            }
        ),
        SimpleNamespace(
            payload={
                "completion_metadata": {
                    "input_tokens": 20,
                    "output_tokens": 3,
                    "cached_tokens": 12,
                }
            }
        ),
    ]

    assert usage_metrics(turns) == {
        "gross_input_tokens": 30,
        "cached_input_tokens": 18,
        "uncached_input_tokens": 12,
        "cache_reporting_complete": True,
        "output_tokens": 5,
    }
    turns[1].payload["completion_metadata"]["cached_tokens"] = None
    unknown = usage_metrics(turns)
    assert unknown["cached_input_tokens"] is None
    assert unknown["uncached_input_tokens"] is None
    assert unknown["cache_reporting_complete"] is False


def test_run_summary_aggregates_parent_and_child_usage():
    turns = [
        SimpleNamespace(
            kind="turn_metrics",
            payload={
                "completion_metadata": {
                    "input_tokens": 30,
                    "output_tokens": 4,
                    "cached_tokens": 20,
                }
            },
        )
    ]
    projection = SimpleNamespace(
        model_request_count=1,
        executed_tool_count=2,
        run_duration_ms=50,
    )

    assert summarize_runs(((turns, projection), (turns, projection))) == {
        "run_count": 2,
        "model_request_count": 2,
        "executed_tool_count": 4,
        "sum_duration_ms": 100,
        "max_duration_ms": 50,
        "gross_input_tokens": 60,
        "cached_input_tokens": 40,
        "uncached_input_tokens": 20,
        "cache_reporting_complete": True,
        "output_tokens": 8,
    }
