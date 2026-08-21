#!/usr/bin/env python3
"""Run deterministic Runtime evaluations and fail closed on regressions."""

import json

from pico.evaluation.evaluator import run_harness_regression_v3
from pico.evaluation.gates import evaluation_failures
from pico.evaluation.metrics import (
    run_context_governance_evaluation,
    run_project_memory_evaluation,
    run_repo_map_evaluation,
    write_runtime_report,
)


def main():
    results = {
        "harness": run_harness_regression_v3(),
        "context": run_context_governance_evaluation(),
        "project_memory": run_project_memory_evaluation(),
        "repo_map": run_repo_map_evaluation(),
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
