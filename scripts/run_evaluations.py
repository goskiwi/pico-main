#!/usr/bin/env python3
"""Run deterministic Runtime evaluations and fail closed on regressions."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.evaluator import run_harness_regression
from evals.gates import evaluation_failures
from evals.metrics import (
    run_context_governance_evaluation,
    run_project_memory_evaluation,
    run_repo_map_evaluation,
    write_runtime_report,
)


def main():
    results = {
        "harness": run_harness_regression(),
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
