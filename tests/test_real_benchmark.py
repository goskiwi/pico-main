import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from evaluation.real_benchmark import (
    _variant_feature_flags,
    build_real_model_client,
    load_real_benchmark,
    RealWorldBenchmarkRunner,
    validate_real_benchmark,
)
from evaluation.real_benchmark_contract import VARIANT_FULL, VARIANT_NO_REPO_MAP
from evaluation.real_benchmark_evidence import _attempt_trace_metrics
from pico.models import FakeModelClient
from tests.helpers import UnitTestSandbox


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DELEGATE_BENCHMARK_PATH = PROJECT_ROOT / "benchmarks" / "real_world_tasks_delegate.json"


def test_repo_map_ablation_disables_only_repo_map_retrieval():
    full = _variant_feature_flags(VARIANT_FULL)
    ablated = _variant_feature_flags(VARIANT_NO_REPO_MAP)

    assert full.get("repo_map", True) is True
    assert ablated["repo_map"] is False
    assert ablated["require_explicit_final"] is True
    assert ablated["require_workspace_change"] is True


def test_delegate_live_regression_requires_delegate_many():
    benchmark = load_real_benchmark(DELEGATE_BENCHMARK_PATH, PROJECT_ROOT)

    assert benchmark["name"] == "pico-delegate-live-regression"
    assert len(benchmark["tasks"]) == 1
    task = benchmark["tasks"][0]
    assert task["required_tools"] == ["delegate_many"]
    assert task["require_successful_delegates"] is True
    assert task["expected_delegate_runs"] == 2
    assert task["expected_delegate_attempts"] == 1
    assert "delegate_many" in task["allowed_tools"]


def test_real_benchmark_client_uses_only_the_supplied_workspace_env():
    env = {
        "OPENAI_API_BASE": "https://env.example/v1",
        "OPENAI_API_KEY": "file-key",
    }

    with patch("evaluation.real_benchmark.OpenAICompatibleModelClient") as client_class:
        client = build_real_model_client("file-model", env=env)

    assert client is client_class.return_value
    assert client_class.call_args.kwargs["model"] == "file-model"
    assert client_class.call_args.kwargs["base_url"] == "https://env.example/v1"
    assert client_class.call_args.kwargs["api_key"] == "file-key"


def test_real_benchmark_runner_rejects_unsupported_provider(tmp_path):
    with pytest.raises(ValueError, match="provider must be 'openai'"):
        RealWorldBenchmarkRunner(
            benchmark_path=DELEGATE_BENCHMARK_PATH,
            artifact_path=tmp_path / "artifact.json",
            report_path=tmp_path / "report.md",
            workspace_root=tmp_path / "workspaces",
            provider="anthropic",
        )


def test_benchmark_rejects_required_tools_not_in_the_allowed_set():
    payload = json.loads(DELEGATE_BENCHMARK_PATH.read_text(encoding="utf-8"))
    payload["tasks"][0]["allowed_tools"].remove("delegate_many")

    with pytest.raises(ValueError, match="required_tools are not allowed"):
        validate_real_benchmark(payload, PROJECT_ROOT)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("expected_delegate_runs", True, "must be an integer"),
        ("expected_delegate_runs", 0, "must be positive"),
        ("expected_delegate_attempts", "1", "must be an integer"),
        ("expected_delegate_attempts", 0, "must be positive"),
    ],
)
def test_benchmark_rejects_invalid_delegate_evidence_expectations(
    field, value, message
):
    payload = json.loads(DELEGATE_BENCHMARK_PATH.read_text(encoding="utf-8"))
    payload["tasks"][0][field] = value

    with pytest.raises(ValueError, match=message):
        validate_real_benchmark(payload, PROJECT_ROOT)


def test_benchmark_delegate_expectations_require_successful_delegates():
    payload = json.loads(DELEGATE_BENCHMARK_PATH.read_text(encoding="utf-8"))
    payload["tasks"][0]["require_successful_delegates"] = False

    with pytest.raises(ValueError, match="requires require_successful_delegates=true"):
        validate_real_benchmark(payload, PROJECT_ROOT)


def test_run_task_skips_hidden_verifier_when_isolation_audit_fails(
    tmp_path,
):
    runner = RealWorldBenchmarkRunner(
        benchmark_path=DELEGATE_BENCHMARK_PATH,
        artifact_path=tmp_path / "artifact.json",
        report_path=tmp_path / "report.md",
        workspace_root=tmp_path / "workspaces",
    )
    runner._shared_model_client = FakeModelClient(
        [
            '<tool>{"name":"patch_file","args":{"path":"settings.py","old_text":"    return label\\n","new_text":"    return label.strip().upper()\\n"}}</tool>',
            "<final>Updated the implementation.</final>",
        ]
    )
    runner._sandbox = lambda workspace_root: UnitTestSandbox(workspace_root)
    task = load_real_benchmark(DELEGATE_BENCHMARK_PATH, PROJECT_ROOT)["tasks"][0]
    failed_audit = {
        "ok": False,
        "expected_workspace_root": "fixture",
        "run_count": 1,
        "run_ids": ["run_main"],
        "violations": [{"type": "workspace_root_mismatch", "run_id": "run_main"}],
    }

    with (
        patch(
            "evaluation.real_benchmark_evidence._workspace_isolation_audit",
            return_value=failed_audit,
        ),
        patch.object(runner, "_verify") as verify,
    ):
        row = runner.run_task(task, variant=VARIANT_FULL, repetition=1)

    verify.assert_not_called()
    assert row["passed"] is False
    assert row["failure_category"] == "workspace_isolation_failed"
    assert row["workspace_isolation"] == failed_audit
    assert row["verifier"]["skipped"] is True
    assert row["verifier"]["exit_code"] == 125
    assert row["parent_model_calls"] == 2
    assert row["delegate_model_calls"] == 0
    assert row["total_model_calls"] == 2
    assert row["model_calls"] == row["total_model_calls"]
    assert row["input_tokens"] == row["total_input_tokens"]
    assert row["output_tokens"] == row["total_output_tokens"]
    assert row["cached_tokens"] == row["total_cached_tokens"]
    assert row["model_failures"] == row["total_model_failures"] == 0
    assert row["model_action_rejections"] == row["total_model_action_rejections"]
    assert row["action_protocols"] == row["total_action_protocols"]
    assert row["parent_action_protocols"] == row["action_protocols"]
    assert row["delegate_action_protocols"] == []
    assert row["delegate_run_count"] == 0


def test_run_task_fails_closed_when_trace_metrics_report_parse_error(tmp_path):
    benchmark_path = PROJECT_ROOT / "benchmarks" / "real_world_tasks.json"
    runner = RealWorldBenchmarkRunner(
        benchmark_path=benchmark_path,
        artifact_path=tmp_path / "artifact.json",
        report_path=tmp_path / "report.md",
        workspace_root=tmp_path / "workspaces",
    )
    runner._shared_model_client = FakeModelClient(
        [
            '<tool>{"name":"patch_file","args":{"path":"inventory.py","old_text":"    return cleaned\\n","new_text":"    return cleaned.upper()\\n"}}</tool>',
            "<final>Updated the implementation.</final>",
        ]
    )
    runner._sandbox = lambda workspace_root: UnitTestSandbox(workspace_root)
    task = load_real_benchmark(benchmark_path, PROJECT_ROOT)["tasks"][0]

    def metrics_with_parse_error(*args, **kwargs):
        metrics = _attempt_trace_metrics(*args, **kwargs)
        error = {
            "run_id": Path(args[0]).name,
            "line_number": 99,
            "error": "truncated JSON",
        }
        metrics["parent"]["trace_parse_errors"] = [error]
        metrics["total"]["trace_parse_errors"] = [error]
        return metrics

    clean_audit = {
        "ok": True,
        "expected_workspace_root": "fixture",
        "run_count": 1,
        "run_ids": ["run_main"],
        "violations": [],
    }
    with (
        patch(
            "evaluation.real_benchmark_evidence._attempt_trace_metrics",
            side_effect=metrics_with_parse_error,
        ),
        patch(
            "evaluation.real_benchmark_evidence._workspace_isolation_audit",
            return_value=clean_audit,
        ),
        patch.object(
            runner,
            "_verify",
            return_value=SimpleNamespace(
                returncode=0, timed_out=False, stdout="", stderr=""
            ),
        ),
    ):
        row = runner.run_task(task, variant=VARIANT_FULL, repetition=1)

    assert row["passed"] is False
    assert row["failure_category"] == "trace_parse_error"
    assert row["trace_parse_errors"][0]["line_number"] == 99


def test_run_task_cross_checks_exact_delegate_attempt_and_child_runs(tmp_path):
    runner = RealWorldBenchmarkRunner(
        benchmark_path=DELEGATE_BENCHMARK_PATH,
        artifact_path=tmp_path / "artifact.json",
        report_path=tmp_path / "report.md",
        workspace_root=tmp_path / "workspaces",
    )
    runner._shared_model_client = FakeModelClient(
        [
            '<tool>{"name":"delegate_many","args":{"tasks":[{"role":"explore","task":"inspect settings.py","max_steps":2},{"role":"review","task":"inspect tests","max_steps":2}]}}</tool>',
            "<final>Explorer completed.</final>",
            "<final>Reviewer completed.</final>",
            '<tool>{"name":"patch_file","args":{"path":"settings.py","old_text":"    return label\\n","new_text":"    cleaned = label.strip()\\n    if not cleaned:\\n        raise ValueError(\\"label must not be empty\\")\\n    return cleaned.upper()\\n"}}</tool>',
            "<final>Updated and verified.</final>",
        ]
    )
    runner._sandbox = lambda workspace_root: UnitTestSandbox(workspace_root)
    task = load_real_benchmark(DELEGATE_BENCHMARK_PATH, PROJECT_ROOT)["tasks"][0]

    with patch.object(
        runner,
        "_verify",
        return_value=SimpleNamespace(
            returncode=0, timed_out=False, stdout="4 passed", stderr=""
        ),
    ):
        row = runner.run_task(task, variant=VARIANT_FULL, repetition=1)

    assert row["passed"] is True
    assert row["delegate_run_count"] == 2
    assert len(row["delegate_agent_ids"]) == 2
    assert row["delegate_evidence"]["ok"] is True
    assert row["delegate_evidence"]["attempt_count"] == 1
    assert row["delegate_evidence"]["requested_count"] == 2
    assert row["delegate_evidence"]["completed_count"] == 2
    assert (
        row["delegate_evidence"]["reported_agent_ids"]
        == row["delegate_evidence"]["related_agent_ids"]
    )
