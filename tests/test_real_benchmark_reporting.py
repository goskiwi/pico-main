import pytest

from evaluation.real_benchmark_contract import VARIANT_FULL, VARIANT_NO_REPO_MAP
from evaluation.real_benchmark_reporting import (
    compare_real_benchmark_artifacts,
    render_real_benchmark_markdown,
    summarize_real_rows,
)


def _benchmark_row(**updates):
    row = {
        "task_id": "task-one",
        "category": "regression",
        "variant": VARIANT_FULL,
        "repetition": 1,
        "passed": True,
        "failure_category": "",
        "tool_steps": 1,
        "model_calls": 1,
        "model_action_rejections": 0,
        "input_tokens": 10,
        "output_tokens": 2,
        "cached_tokens": 3,
        "agent_duration_ms": 100,
        "total_duration_ms": 120,
        "action_protocols": ["responses_api"],
    }
    row.update(updates)
    return row


def _benchmark_artifact(row, *, schema_version):
    return {
        "schema_version": schema_version,
        "captured_at": "2026-07-22T00:00:00+00:00",
        "execution_mode": "live_llm",
        "provider": "openai",
        "model": "test-model",
        "runtime": {"commit_sha": "abc123", "working_tree_dirty": False},
        "benchmark": {
            "name": "test benchmark",
            "task_count": 1,
            "fixture_snapshot_id": "sha256:fixture",
            "evaluation_snapshot_id": "sha256:evaluation",
        },
        "run_config": {},
        "sandbox": {
            "image": "test-image",
            "cpus": 1,
            "memory": "1g",
            "pids_limit": 32,
        },
        "repetitions": 1,
        "summary": summarize_real_rows([row]),
        "rows": [row],
    }


def test_summary_and_report_expose_parent_delegate_total_costs():
    row = _benchmark_row(
        parent_model_calls=1,
        delegate_model_calls=2,
        total_model_calls=3,
        model_calls=3,
        parent_input_tokens=10,
        delegate_input_tokens=20,
        total_input_tokens=30,
        input_tokens=30,
        parent_output_tokens=2,
        delegate_output_tokens=4,
        total_output_tokens=6,
        output_tokens=6,
        parent_cached_tokens=3,
        delegate_cached_tokens=5,
        total_cached_tokens=8,
        cached_tokens=8,
        parent_model_duration_ms=100,
        delegate_model_duration_ms=250,
        total_model_duration_ms=350,
        parent_model_failures=1,
        delegate_model_failures=2,
        total_model_failures=3,
        model_failures=3,
        parent_model_action_rejections=1,
        delegate_model_action_rejections=2,
        total_model_action_rejections=3,
        model_action_rejections=3,
        parent_action_protocols=["parent_protocol"],
        delegate_action_protocols=["child_protocol"],
        total_action_protocols=["child_protocol", "parent_protocol"],
        action_protocols=["child_protocol", "parent_protocol"],
        delegate_run_count=2,
    )
    artifact = _benchmark_artifact(row, schema_version=3)
    artifact["run_config"]["model_cost_scope"] = "attempt_parent_and_related_delegates"

    metrics = artifact["summary"]["variants"][VARIANT_FULL]
    report = render_real_benchmark_markdown(artifact)

    assert metrics["avg_parent_model_calls"] == 1
    assert metrics["avg_delegate_model_calls"] == 2
    assert metrics["avg_total_model_calls"] == 3
    assert metrics["avg_model_calls"] == 3
    assert metrics["total_parent_input_tokens"] == 10
    assert metrics["total_delegate_input_tokens"] == 20
    assert metrics["total_input_tokens"] == 30
    assert metrics["delegate_run_count"] == 2
    assert metrics["avg_delegate_run_count"] == 2
    assert metrics["total_delegate_run_count"] == 2
    assert metrics["avg_parent_model_failures"] == 1
    assert metrics["avg_delegate_model_failures"] == 2
    assert metrics["avg_total_model_failures"] == 3
    assert metrics["avg_model_failures"] == 3
    assert metrics["avg_parent_model_action_rejections"] == 1
    assert metrics["avg_delegate_model_action_rejections"] == 2
    assert metrics["avg_total_model_action_rejections"] == 3
    assert metrics["avg_model_action_rejections"] == 3
    assert metrics["parent_action_protocols"] == ["parent_protocol"]
    assert metrics["delegate_action_protocols"] == ["child_protocol"]
    assert metrics["action_protocols"] == ["child_protocol", "parent_protocol"]
    assert "Avg calls P/D/T" in report
    assert "Avg delegates" in report
    assert "Avg failures P/D/T" in report
    assert "Avg rejects P/D/T" in report
    assert "child_protocol, parent_protocol" in report
    assert "1.00/2.00/3.00" in report
    assert "10/20/30" in report
    assert "0.10s/0.25s/0.35s" in report


def test_summary_and_report_compare_repo_map_ablation():
    full = _benchmark_row(variant=VARIANT_FULL, passed=True, tool_steps=2)
    ablated = _benchmark_row(
        variant=VARIANT_NO_REPO_MAP,
        passed=False,
        tool_steps=5,
    )

    summary = summarize_real_rows([full, ablated])
    artifact = _benchmark_artifact(full, schema_version=3)
    artifact["rows"] = [full, ablated]
    artifact["summary"] = summary
    report = render_real_benchmark_markdown(artifact)

    assert summary["comparison"]["repo_map_pass_rate_delta"] == 1.0
    assert summary["comparison"]["repo_map_avg_tool_steps_delta"] == -3.0
    assert "full - no_repo_map" in report


def test_report_keeps_schema_v2_parent_only_artifacts_renderable():
    artifact = _benchmark_artifact(_benchmark_row(), schema_version=2)

    report = render_real_benchmark_markdown(artifact)
    comparison = compare_real_benchmark_artifacts(artifact, artifact)

    assert "Model cost scope: `parent_run_only`" in report
    assert "1.00/0.00/1.00" in report
    assert "10/0/10" in report
    assert comparison["model_cost_scope"] == "parent_run_only"
    assert comparison["summary"]["baseline_avg_model_calls"] == 1


def test_comparison_rejects_mixed_parent_only_and_attempt_total_cost_scopes():
    row = _benchmark_row()
    baseline = _benchmark_artifact(row, schema_version=2)
    candidate = _benchmark_artifact(row, schema_version=3)

    with pytest.raises(ValueError, match="model-cost scopes do not match"):
        compare_real_benchmark_artifacts(baseline, candidate)
