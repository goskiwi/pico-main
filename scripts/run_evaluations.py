#!/usr/bin/env python3
"""Run deterministic Runtime evaluations and write replayable artifacts."""

from pico.evaluation.evaluator import run_harness_regression_v3
from pico.evaluation.metrics import (
    run_context_governance_ablation,
    run_project_memory_evaluation,
    run_repo_map_evaluation,
    run_runtime_governance_evaluation,
    run_working_memory_ablation,
    write_runtime_report,
)


def main():
    run_harness_regression_v3()
    run_context_governance_ablation()
    run_working_memory_ablation()
    run_project_memory_evaluation()
    run_repo_map_evaluation()
    run_runtime_governance_evaluation()
    write_runtime_report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
