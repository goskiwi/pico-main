"""Artifact aggregation, comparison, and Markdown rendering."""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from .common import safe_mean as _safe_mean
from .common import safe_ratio as _safe_ratio
from .common import utc_timestamp as _utc_timestamp
from .real_benchmark_contract import (
    SUPPORTED_VARIANTS,
    VARIANT_FULL,
    VARIANT_NO_MEMORY_CONTEXT,
)


def _scoped_row_metric(row, scope, metric):
    """Read schema-v3 aggregate fields with a parent-only fallback for old rows."""
    scoped_key = f"{scope}_{metric}"
    if scoped_key in row:
        return int(row[scoped_key])
    if scope == "delegate":
        return 0
    return int(row.get(metric, 0))


def _scoped_row_protocols(row, scope):
    scoped_key = f"{scope}_action_protocols"
    if scoped_key in row:
        return list(row[scoped_key])
    if scope == "delegate":
        return []
    return list(row.get("action_protocols", []))


def summarize_real_rows(rows):
    rows = list(rows)
    variants = {}
    for variant in SUPPORTED_VARIANTS:
        variant_rows = [row for row in rows if row["variant"] == variant]
        if not variant_rows:
            continue
        repetition_summaries = []
        for repetition in sorted(
            {int(row.get("repetition", 1)) for row in variant_rows}
        ):
            repetition_rows = [
                row
                for row in variant_rows
                if int(row.get("repetition", 1)) == repetition
            ]
            passed = sum(1 for row in repetition_rows if row["passed"])
            repetition_summaries.append(
                {
                    "repetition": repetition,
                    "attempt_count": len(repetition_rows),
                    "passed": passed,
                    "pass_rate": _safe_ratio(passed, len(repetition_rows)),
                    "avg_tool_steps": _safe_mean(
                        row["tool_steps"] for row in repetition_rows
                    ),
                    "avg_model_calls": _safe_mean(
                        _scoped_row_metric(row, "total", "model_calls")
                        for row in repetition_rows
                    ),
                    "avg_total_duration_ms": _safe_mean(
                        row["total_duration_ms"] for row in repetition_rows
                    ),
                }
            )
        task_stability = []
        for task_id in sorted({str(row["task_id"]) for row in variant_rows}):
            task_rows = [row for row in variant_rows if str(row["task_id"]) == task_id]
            passed = sum(1 for row in task_rows if row["passed"])
            if passed == len(task_rows):
                outcome = "always_passed"
            elif passed == 0:
                outcome = "always_failed"
            else:
                outcome = "mixed"
            task_stability.append(
                {
                    "task_id": task_id,
                    "category": task_rows[0]["category"],
                    "attempt_count": len(task_rows),
                    "passed": passed,
                    "pass_rate": _safe_ratio(passed, len(task_rows)),
                    "outcome": outcome,
                }
            )
        repetition_pass_rates = [item["pass_rate"] for item in repetition_summaries]
        passed = sum(1 for row in variant_rows if row["passed"])
        variants[variant] = {
            "task_count": len(task_stability),
            "attempt_count": len(variant_rows),
            "repetition_count": len(repetition_summaries),
            "passed": passed,
            "pass_rate": _safe_ratio(passed, len(variant_rows)),
            "repetition_pass_rate_mean": _safe_mean(repetition_pass_rates),
            "repetition_pass_rate_stddev": (
                statistics.pstdev(repetition_pass_rates)
                if len(repetition_pass_rates) > 1
                else 0.0
            ),
            "repetition_pass_rate_min": min(repetition_pass_rates),
            "repetition_pass_rate_max": max(repetition_pass_rates),
            "complete_repetitions": sum(
                1
                for item in repetition_summaries
                if item["passed"] == item["attempt_count"]
            ),
            "repetition_summaries": repetition_summaries,
            "task_stability": task_stability,
            "avg_tool_steps": _safe_mean(row["tool_steps"] for row in variant_rows),
            "avg_parent_model_calls": _safe_mean(
                _scoped_row_metric(row, "parent", "model_calls") for row in variant_rows
            ),
            "avg_delegate_model_calls": _safe_mean(
                _scoped_row_metric(row, "delegate", "model_calls")
                for row in variant_rows
            ),
            "avg_total_model_calls": _safe_mean(
                _scoped_row_metric(row, "total", "model_calls") for row in variant_rows
            ),
            "avg_model_calls": _safe_mean(
                _scoped_row_metric(row, "total", "model_calls") for row in variant_rows
            ),
            "avg_parent_model_failures": _safe_mean(
                _scoped_row_metric(row, "parent", "model_failures")
                for row in variant_rows
            ),
            "avg_delegate_model_failures": _safe_mean(
                _scoped_row_metric(row, "delegate", "model_failures")
                for row in variant_rows
            ),
            "avg_total_model_failures": _safe_mean(
                _scoped_row_metric(row, "total", "model_failures")
                for row in variant_rows
            ),
            "avg_model_failures": _safe_mean(
                _scoped_row_metric(row, "total", "model_failures")
                for row in variant_rows
            ),
            "avg_parent_model_action_rejections": _safe_mean(
                _scoped_row_metric(row, "parent", "model_action_rejections")
                for row in variant_rows
            ),
            "avg_delegate_model_action_rejections": _safe_mean(
                _scoped_row_metric(row, "delegate", "model_action_rejections")
                for row in variant_rows
            ),
            "avg_total_model_action_rejections": _safe_mean(
                _scoped_row_metric(row, "total", "model_action_rejections")
                for row in variant_rows
            ),
            "avg_model_action_rejections": _safe_mean(
                _scoped_row_metric(row, "total", "model_action_rejections")
                for row in variant_rows
            ),
            "avg_agent_duration_ms": _safe_mean(
                row["agent_duration_ms"] for row in variant_rows
            ),
            "avg_total_duration_ms": _safe_mean(
                row["total_duration_ms"] for row in variant_rows
            ),
            "avg_delegate_run_count": _safe_mean(
                int(row.get("delegate_run_count", 0)) for row in variant_rows
            ),
            "total_delegate_run_count": sum(
                int(row.get("delegate_run_count", 0)) for row in variant_rows
            ),
            "delegate_run_count": sum(
                int(row.get("delegate_run_count", 0)) for row in variant_rows
            ),
            "total_parent_input_tokens": sum(
                _scoped_row_metric(row, "parent", "input_tokens")
                for row in variant_rows
            ),
            "total_delegate_input_tokens": sum(
                _scoped_row_metric(row, "delegate", "input_tokens")
                for row in variant_rows
            ),
            "total_input_tokens": sum(
                _scoped_row_metric(row, "total", "input_tokens") for row in variant_rows
            ),
            "total_parent_output_tokens": sum(
                _scoped_row_metric(row, "parent", "output_tokens")
                for row in variant_rows
            ),
            "total_delegate_output_tokens": sum(
                _scoped_row_metric(row, "delegate", "output_tokens")
                for row in variant_rows
            ),
            "total_output_tokens": sum(
                _scoped_row_metric(row, "total", "output_tokens")
                for row in variant_rows
            ),
            "total_parent_cached_tokens": sum(
                _scoped_row_metric(row, "parent", "cached_tokens")
                for row in variant_rows
            ),
            "total_delegate_cached_tokens": sum(
                _scoped_row_metric(row, "delegate", "cached_tokens")
                for row in variant_rows
            ),
            "total_cached_tokens": sum(
                _scoped_row_metric(row, "total", "cached_tokens")
                for row in variant_rows
            ),
            "avg_parent_model_duration_ms": _safe_mean(
                _scoped_row_metric(row, "parent", "model_duration_ms")
                for row in variant_rows
            ),
            "avg_delegate_model_duration_ms": _safe_mean(
                _scoped_row_metric(row, "delegate", "model_duration_ms")
                for row in variant_rows
            ),
            "avg_total_model_duration_ms": _safe_mean(
                _scoped_row_metric(row, "total", "model_duration_ms")
                for row in variant_rows
            ),
            "parent_action_protocols": sorted(
                {
                    protocol
                    for row in variant_rows
                    for protocol in _scoped_row_protocols(row, "parent")
                }
            ),
            "delegate_action_protocols": sorted(
                {
                    protocol
                    for row in variant_rows
                    for protocol in _scoped_row_protocols(row, "delegate")
                }
            ),
            "total_action_protocols": sorted(
                {
                    protocol
                    for row in variant_rows
                    for protocol in _scoped_row_protocols(row, "total")
                }
            ),
            "action_protocols": sorted(
                {
                    protocol
                    for row in variant_rows
                    for protocol in _scoped_row_protocols(row, "total")
                }
            ),
        }
    category_task_ids = {}
    failure_counts = {}
    for row in rows:
        category_task_ids.setdefault(row["category"], set()).add(str(row["task_id"]))
        if row["failure_category"]:
            failure_counts[row["failure_category"]] = (
                failure_counts.get(row["failure_category"], 0) + 1
            )
    comparison = {}
    if VARIANT_FULL in variants and VARIANT_NO_MEMORY_CONTEXT in variants:
        comparison = {
            "pass_rate_delta": variants[VARIANT_FULL]["pass_rate"]
            - variants[VARIANT_NO_MEMORY_CONTEXT]["pass_rate"],
            "avg_tool_steps_delta": variants[VARIANT_FULL]["avg_tool_steps"]
            - variants[VARIANT_NO_MEMORY_CONTEXT]["avg_tool_steps"],
        }
    return {
        "row_count": len(rows),
        "category_counts": {
            category: len(task_ids)
            for category, task_ids in sorted(category_task_ids.items())
        },
        "failure_category_counts": failure_counts,
        "variants": variants,
        "comparison": comparison,
    }


def _artifact_model_cost_scope(artifact):
    configured = str(
        (artifact.get("run_config") or {}).get("model_cost_scope", "")
    ).strip()
    if configured:
        return configured
    if int(artifact.get("schema_version", 0) or 0) >= 3:
        return "attempt_parent_and_related_delegates"
    return "parent_run_only"


def render_real_benchmark_markdown(artifact):
    summary = artifact["summary"]
    benchmark_name = artifact["benchmark"].get("name") or "Pico Real-world Benchmark"
    model_cost_scope = _artifact_model_cost_scope(artifact)
    lines = [
        f"# {benchmark_name}",
        "",
        f"- Captured at: `{artifact['captured_at']}`",
        f"- Provider: `{artifact['provider']}`",
        f"- Model: `{artifact['model']}`",
        f"- Execution mode: `{artifact.get('execution_mode', 'unknown')}`",
        f"- Commit: `{artifact['runtime']['commit_sha'] or 'working-tree'}`",
        f"- Working tree dirty: `{artifact['runtime'].get('working_tree_dirty', 'unknown')}`",
        f"- Tasks: {artifact['benchmark']['task_count']}",
        f"- Repetitions: {artifact['repetitions']}",
        f"- Fixture snapshot: `{artifact['benchmark']['fixture_snapshot_id']}`",
        f"- Evaluation snapshot: `{artifact['benchmark'].get('evaluation_snapshot_id', 'not-recorded')}`",
        (
            f"- Run config: temperature={artifact.get('run_config', {}).get('temperature', 'unknown')}, "
            f"max_new_tokens={artifact.get('run_config', {}).get('max_new_tokens', 'unknown')}, "
            f"verifier_timeout={artifact.get('run_config', {}).get('verifier_timeout_seconds', 'unknown')}s"
        ),
        f"- Model cost scope: `{model_cost_scope}`",
        (
            "- Duration semantics: model time is cumulative across model calls; "
            "agent duration is parent-attempt wall time and already includes delegate wait"
        ),
        (
            f"- Sandbox: `{artifact['sandbox']['image']}`, {artifact['sandbox']['cpus']} CPU, "
            f"{artifact['sandbox']['memory']} memory, {artifact['sandbox']['pids_limit']} PIDs"
        ),
        "",
        "## Results",
        "",
        "| Variant | Protocols (all) | Pass rate | Passed | Avg tools | Avg calls P/D/T | Avg delegates | Avg failures P/D/T | Avg rejects P/D/T | Input P/D/T | Cached P/D/T | Output P/D/T | Model time P/D/T | Avg duration |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant, metrics in summary["variants"].items():
        avg_model_calls = metrics["avg_model_calls"]
        parent_calls = metrics.get("avg_parent_model_calls", avg_model_calls)
        delegate_calls = metrics.get("avg_delegate_model_calls", 0.0)
        total_calls = metrics.get("avg_total_model_calls", avg_model_calls)
        parent_failures = metrics.get("avg_parent_model_failures", 0.0)
        delegate_failures = metrics.get("avg_delegate_model_failures", 0.0)
        total_failures = metrics.get("avg_total_model_failures", parent_failures)
        avg_rejections = metrics.get("avg_model_action_rejections", 0.0)
        parent_rejections = metrics.get(
            "avg_parent_model_action_rejections", avg_rejections
        )
        delegate_rejections = metrics.get("avg_delegate_model_action_rejections", 0.0)
        total_rejections = metrics.get(
            "avg_total_model_action_rejections", avg_rejections
        )
        attempt_count = max(1, int(metrics.get("attempt_count", 1)))
        avg_delegate_runs = metrics.get(
            "avg_delegate_run_count",
            metrics.get("delegate_run_count", 0) / attempt_count,
        )
        parent_input = metrics.get(
            "total_parent_input_tokens", metrics["total_input_tokens"]
        )
        delegate_input = metrics.get("total_delegate_input_tokens", 0)
        parent_cached = metrics.get(
            "total_parent_cached_tokens", metrics["total_cached_tokens"]
        )
        delegate_cached = metrics.get("total_delegate_cached_tokens", 0)
        parent_output = metrics.get(
            "total_parent_output_tokens", metrics["total_output_tokens"]
        )
        delegate_output = metrics.get("total_delegate_output_tokens", 0)
        parent_model_duration_ms = metrics.get("avg_parent_model_duration_ms", 0.0)
        delegate_model_duration_ms = metrics.get("avg_delegate_model_duration_ms", 0.0)
        total_model_duration_ms = metrics.get(
            "avg_total_model_duration_ms", parent_model_duration_ms
        )
        lines.append(
            f"| {variant} | {', '.join(metrics.get('action_protocols', [])) or '-'} "
            f"| {metrics['pass_rate']:.1%} | {metrics['passed']}/{metrics.get('attempt_count', metrics['task_count'])} "
            f"| {metrics['avg_tool_steps']:.2f} "
            f"| {parent_calls:.2f}/{delegate_calls:.2f}/{total_calls:.2f} "
            f"| {avg_delegate_runs:.2f} "
            f"| {parent_failures:.2f}/{delegate_failures:.2f}/{total_failures:.2f} "
            f"| {parent_rejections:.2f}/{delegate_rejections:.2f}/{total_rejections:.2f} "
            f"| {parent_input}/{delegate_input}/{metrics['total_input_tokens']} "
            f"| {parent_cached}/{delegate_cached}/{metrics['total_cached_tokens']} "
            f"| {parent_output}/{delegate_output}/{metrics['total_output_tokens']} "
            f"| {parent_model_duration_ms / 1000:.2f}s/"
            f"{delegate_model_duration_ms / 1000:.2f}s/"
            f"{total_model_duration_ms / 1000:.2f}s "
            f"| {metrics['avg_total_duration_ms'] / 1000:.2f}s |"
        )
    if artifact.get("repetitions", 1) > 1:
        lines.extend(
            [
                "",
                "## Repetition stability",
                "",
                "| Variant | Mean pass rate | Stddev | Min | Max | Complete runs |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for variant, metrics in summary["variants"].items():
            lines.append(
                f"| {variant} | {metrics['repetition_pass_rate_mean']:.1%} "
                f"| {metrics['repetition_pass_rate_stddev']:.1%} "
                f"| {metrics['repetition_pass_rate_min']:.1%} "
                f"| {metrics['repetition_pass_rate_max']:.1%} "
                f"| {metrics['complete_repetitions']}/{metrics['repetition_count']} |"
            )
        lines.extend(
            [
                "",
                "### Per repetition",
                "",
                "| Variant | Repetition | Pass rate | Passed | Avg calls | Avg duration |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for variant, metrics in summary["variants"].items():
            for item in metrics["repetition_summaries"]:
                lines.append(
                    f"| {variant} | {item['repetition']} | {item['pass_rate']:.1%} "
                    f"| {item['passed']}/{item['attempt_count']} "
                    f"| {item['avg_model_calls']:.2f} "
                    f"| {item['avg_total_duration_ms'] / 1000:.2f}s |"
                )
        lines.extend(
            [
                "",
                "### Per-task stability",
                "",
                "| Variant | Task | Pass rate | Passed | Outcome |",
                "|---|---|---:|---:|---|",
            ]
        )
        for variant, metrics in summary["variants"].items():
            for item in metrics["task_stability"]:
                lines.append(
                    f"| {variant} | {item['task_id']} | {item['pass_rate']:.1%} "
                    f"| {item['passed']}/{item['attempt_count']} | {item['outcome']} |"
                )
    if summary["comparison"]:
        lines.extend(
            [
                "",
                "## Ablation",
                "",
                f"- Pass-rate delta (full - no_memory_context): {summary['comparison']['pass_rate_delta']:+.1%}",
                f"- Avg tool-step delta: {summary['comparison']['avg_tool_steps_delta']:+.2f}",
            ]
        )
    if summary["failure_category_counts"]:
        lines.extend(
            [
                "",
                "## Failure breakdown",
                "",
                "| Failure category | Count |",
                "|---|---:|",
            ]
        )
        for category, count in sorted(summary["failure_category_counts"].items()):
            lines.append(f"| {category} | {count} |")
    lines.extend(
        [
            "",
            "## Task details",
            "",
            "| Task | Rep | Category | Variant | Result | Isolation | Tools | Delegates | Calls P/D/T | Failures P/D/T | Rejects P/D/T | Model time P/D/T | Duration | Failure |",
            "|---|---:|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in artifact["rows"]:
        result = "PASS" if row["passed"] else "FAIL"
        isolation = (
            "PASS" if row.get("workspace_isolation", {}).get("ok", True) else "FAIL"
        )
        parent_calls = _scoped_row_metric(row, "parent", "model_calls")
        delegate_calls = _scoped_row_metric(row, "delegate", "model_calls")
        total_calls = _scoped_row_metric(row, "total", "model_calls")
        parent_failures = _scoped_row_metric(row, "parent", "model_failures")
        delegate_failures = _scoped_row_metric(row, "delegate", "model_failures")
        total_failures = _scoped_row_metric(row, "total", "model_failures")
        parent_rejections = _scoped_row_metric(row, "parent", "model_action_rejections")
        delegate_rejections = _scoped_row_metric(
            row, "delegate", "model_action_rejections"
        )
        total_rejections = _scoped_row_metric(row, "total", "model_action_rejections")
        parent_model_duration_ms = _scoped_row_metric(
            row, "parent", "model_duration_ms"
        )
        delegate_model_duration_ms = _scoped_row_metric(
            row, "delegate", "model_duration_ms"
        )
        total_model_duration_ms = _scoped_row_metric(row, "total", "model_duration_ms")
        lines.append(
            f"| {row['task_id']} | {row.get('repetition', 1)} | {row['category']} | {row['variant']} | {result} "
            f"| {isolation} | {row['tool_steps']} "
            f"| {row.get('delegate_run_count', 0)} "
            f"| {parent_calls}/{delegate_calls}/{total_calls} "
            f"| {parent_failures}/{delegate_failures}/{total_failures} "
            f"| {parent_rejections}/{delegate_rejections}/{total_rejections} "
            f"| {parent_model_duration_ms / 1000:.2f}s/"
            f"{delegate_model_duration_ms / 1000:.2f}s/"
            f"{total_model_duration_ms / 1000:.2f}s | "
            f"{row['total_duration_ms'] / 1000:.2f}s "
            f"| {row['failure_category'] or '-'} |"
        )
    lines.extend(
        [
            "",
            "## Scope boundary",
            "",
            "- These are real model runs over fresh repository copies; hidden verifier tests are injected only after the agent stops.",
            "- Parent and child run roots, file-tool paths, search results, and verifier-source exposure are audited before hidden verifier injection; failures skip verification.",
            "- In schema v3, compatibility fields for model calls, tokens, failures, rejections, and protocols cover the parent plus related delegates; explicit P/D/T fields retain the breakdown.",
            "- Required/executed tools and structured delegate outcomes remain parent-trace checks; related child identities and completion are cross-checked from child traces, whose model events also contribute to aggregate behavior and cost metrics.",
            "- Cumulative model-call duration is a workload indicator, not wall latency; concurrent child durations can overlap. Agent duration is the parent attempt's end-to-end wall time.",
            "- Verifiers run inside the mandatory Docker sandbox with networking disabled.",
            "- Results are model-, prompt-, and fixture-snapshot-specific; they are not a universal coding benchmark claim.",
            "- Repeated attempts over the same tasks are not independent task samples; standard deviation is calculated across full-suite repetitions.",
            "",
        ]
    )
    return "\n".join(lines)


def compare_real_benchmark_artifacts(baseline, candidate):
    """Compare two runs over the exact same benchmark snapshot and task set."""
    baseline = _load_artifact_value(baseline)
    candidate = _load_artifact_value(candidate)
    baseline_benchmark = baseline.get("benchmark") or {}
    candidate_benchmark = candidate.get("benchmark") or {}
    baseline_snapshot = baseline_benchmark.get(
        "evaluation_snapshot_id"
    ) or baseline_benchmark.get("fixture_snapshot_id")
    candidate_snapshot = candidate_benchmark.get(
        "evaluation_snapshot_id"
    ) or candidate_benchmark.get("fixture_snapshot_id")
    if not baseline_snapshot or baseline_snapshot != candidate_snapshot:
        raise ValueError("benchmark evaluation snapshots do not match")
    if baseline.get("provider") != candidate.get("provider"):
        raise ValueError("benchmark providers do not match")
    if baseline.get("model") != candidate.get("model"):
        raise ValueError("benchmark models do not match")
    baseline_cost_scope = _artifact_model_cost_scope(baseline)
    candidate_cost_scope = _artifact_model_cost_scope(candidate)
    if baseline_cost_scope != candidate_cost_scope:
        raise ValueError("benchmark model-cost scopes do not match")
    baseline_rows = _full_rows_by_task(baseline)
    candidate_rows = _full_rows_by_task(candidate)
    if set(baseline_rows) != set(candidate_rows):
        raise ValueError("benchmark task sets do not match")

    task_rows = []
    for task_id in sorted(baseline_rows):
        before = baseline_rows[task_id]
        after = candidate_rows[task_id]
        task_rows.append(
            {
                "task_id": task_id,
                "baseline_passed": bool(before["passed"]),
                "candidate_passed": bool(after["passed"]),
                "pass_change": int(bool(after["passed"])) - int(bool(before["passed"])),
                "baseline_model_calls": int(before["model_calls"]),
                "candidate_model_calls": int(after["model_calls"]),
                "model_calls_delta": int(after["model_calls"])
                - int(before["model_calls"]),
                "baseline_action_rejections": (
                    int(before["model_action_rejections"])
                    if "model_action_rejections" in before
                    else None
                ),
                "candidate_action_rejections": (
                    int(after["model_action_rejections"])
                    if "model_action_rejections" in after
                    else None
                ),
            }
        )
    count = len(task_rows)
    baseline_passed = sum(row["baseline_passed"] for row in task_rows)
    candidate_passed = sum(row["candidate_passed"] for row in task_rows)
    return {
        "schema_version": 1,
        "artifact_type": "real-world-benchmark-comparison",
        "captured_at": _utc_timestamp(),
        "provider": baseline.get("provider", ""),
        "model": baseline.get("model", ""),
        "model_cost_scope": baseline_cost_scope,
        "evaluation_snapshot_id": baseline_snapshot,
        "snapshot_type": (
            "evaluation"
            if baseline_benchmark.get("evaluation_snapshot_id")
            else "fixture_legacy"
        ),
        "task_count": count,
        "summary": {
            "baseline_pass_rate": _safe_ratio(baseline_passed, count),
            "candidate_pass_rate": _safe_ratio(candidate_passed, count),
            "pass_rate_delta": _safe_ratio(candidate_passed - baseline_passed, count),
            "baseline_avg_model_calls": _safe_mean(
                row["baseline_model_calls"] for row in task_rows
            ),
            "candidate_avg_model_calls": _safe_mean(
                row["candidate_model_calls"] for row in task_rows
            ),
            "avg_model_calls_delta": _safe_mean(
                row["model_calls_delta"] for row in task_rows
            ),
            "baseline_action_rejections": _optional_sum(
                row["baseline_action_rejections"] for row in task_rows
            ),
            "candidate_action_rejections": _optional_sum(
                row["candidate_action_rejections"] for row in task_rows
            ),
        },
        "rows": task_rows,
    }


def _load_artifact_value(value):
    if isinstance(value, dict):
        return value
    return json.loads(Path(value).read_text(encoding="utf-8"))


def _full_rows_by_task(artifact):
    full_rows = [
        row for row in artifact.get("rows", []) if row.get("variant") == VARIANT_FULL
    ]
    if any(int(row.get("repetition", 1)) != 1 for row in full_rows):
        raise ValueError("comparison accepts only single-repetition artifacts")
    rows = full_rows
    result = {str(row["task_id"]): row for row in rows}
    if len(result) != len(rows) or not result:
        raise ValueError("comparison needs one full-variant row per task")
    return result


def _optional_sum(values):
    values = list(values)
    if not values or any(value is None for value in values):
        return None
    return sum(values)


def render_real_benchmark_comparison_markdown(comparison):
    summary = comparison["summary"]
    baseline_rejections = summary["baseline_action_rejections"]
    candidate_rejections = summary["candidate_action_rejections"]
    rejection_delta = (
        f"{candidate_rejections - baseline_rejections:+d}"
        if baseline_rejections is not None and candidate_rejections is not None
        else "n/a"
    )
    lines = [
        "# Structured Action Protocol Comparison",
        "",
        f"- Captured at: `{comparison['captured_at']}`",
        f"- Provider: `{comparison.get('provider', 'not-recorded')}`",
        f"- Model: `{comparison['model']}`",
        f"- Model cost scope: `{comparison.get('model_cost_scope', 'parent_run_only')}`",
        f"- Matched tasks: {comparison['task_count']}",
        f"- Snapshot ({comparison.get('snapshot_type', 'unknown')}): `{comparison['evaluation_snapshot_id']}`",
        "",
        "| Metric | Text protocol | Structured actions | Delta |",
        "|---|---:|---:|---:|",
        f"| Pass rate | {summary['baseline_pass_rate']:.1%} | {summary['candidate_pass_rate']:.1%} | {summary['pass_rate_delta']:+.1%} |",
        f"| Avg model calls | {summary['baseline_avg_model_calls']:.2f} | {summary['candidate_avg_model_calls']:.2f} | {summary['avg_model_calls_delta']:+.2f} |",
        f"| Action rejections | {baseline_rejections if baseline_rejections is not None else 'not recorded'} "
        f"| {candidate_rejections if candidate_rejections is not None else 'not recorded'} "
        f"| {rejection_delta} |",
        "",
        "## Task details",
        "",
        "| Task | Text | Structured | Calls before | Calls after |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in comparison["rows"]:
        lines.append(
            f"| {row['task_id']} | {'PASS' if row['baseline_passed'] else 'FAIL'} "
            f"| {'PASS' if row['candidate_passed'] else 'FAIL'} "
            f"| {row['baseline_model_calls']} | {row['candidate_model_calls']} |"
        )
    lines.extend(
        [
            "",
            "The comparison is accepted only when provider, model, task IDs, and the full evaluation snapshot are identical.",
            "",
        ]
    )
    return "\n".join(lines)
