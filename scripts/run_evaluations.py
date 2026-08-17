#!/usr/bin/env python3
"""Run deterministic Runtime evaluations and fail closed on regressions."""

import json

from pico.evaluation.evaluator import run_harness_regression_v3
from pico.evaluation.metrics import (
    run_context_governance_ablation,
    run_project_memory_evaluation,
    run_repo_map_evaluation,
    run_runtime_governance_evaluation,
    run_working_memory_ablation,
    write_runtime_report,
)


def evaluation_failures(results):
    failures = []
    harness = results["harness"]["summary"]
    if harness["passed"] != harness["total_tasks"]:
        failures.append(
            f"native Harness passed {harness['passed']}/{harness['total_tasks']}"
        )

    context = results["context"]["summary"]
    if context["within_budget_rate"] != 1.0 or context["current_request_preserved_rate"] != 1.0:
        failures.append("context governance invariant failed")

    working = results["working_memory"]["variants"]
    if working["memory_on"]["hit_rate"] != 1.0 or working["stale_revision"]["hit_rate"] != 0.0:
        failures.append("working-memory freshness invariant failed")

    for name in ("project_memory", "repo_map", "runtime_governance"):
        failed_checks = [
            key for key, passed in results[name]["summary"].items() if passed is not True
        ]
        if failed_checks:
            failures.append(f"{name} failed checks: {', '.join(failed_checks)}")
    return failures


def main():
    results = {
        "harness": run_harness_regression_v3(),
        "context": run_context_governance_ablation(),
        "working_memory": run_working_memory_ablation(),
        "project_memory": run_project_memory_evaluation(),
        "repo_map": run_repo_map_evaluation(),
        "runtime_governance": run_runtime_governance_evaluation(),
    }
    write_runtime_report()
    failures = evaluation_failures(results)
    summary = {
        "status": "failed" if failures else "passed",
        "failures": failures,
        "harness": results["harness"]["summary"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
