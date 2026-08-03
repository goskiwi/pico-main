#!/usr/bin/env python3
"""Run Pico's deterministic paired runtime-contract benchmark."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pico.evaluation.runtime_contract_benchmark import (  # noqa: E402
    DEFAULT_RUNTIME_CONTRACT_ARTIFACT_PATH,
    DEFAULT_RUNTIME_CONTRACT_BENCHMARK_PATH,
    DEFAULT_RUNTIME_CONTRACT_REPORT_PATH,
    DEFAULT_RUNTIME_CONTRACT_WORKSPACE_ROOT,
    RuntimeContractBenchmarkRunner,
)


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Run Pico's deterministic paired runtime-contract benchmark. "
            "No remote model calls are made."
        )
    )
    parser.add_argument(
        "--benchmark-path",
        default=str(DEFAULT_RUNTIME_CONTRACT_BENCHMARK_PATH),
    )
    parser.add_argument(
        "--artifact-path",
        default=str(DEFAULT_RUNTIME_CONTRACT_ARTIFACT_PATH),
    )
    parser.add_argument(
        "--report-path",
        default=str(DEFAULT_RUNTIME_CONTRACT_REPORT_PATH),
    )
    parser.add_argument(
        "--workspace-root",
        default=str(DEFAULT_RUNTIME_CONTRACT_WORKSPACE_ROOT),
    )
    parser.add_argument("--case", action="append", dest="task_ids")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument(
        "--require-clean-worktree",
        action="store_true",
        help="Refuse to run when tracked or untracked files are present.",
    )
    return parser


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    runner = RuntimeContractBenchmarkRunner(
        benchmark_path=args.benchmark_path,
        artifact_path=args.artifact_path,
        report_path=args.report_path,
        workspace_root=args.workspace_root,
        repetitions=args.repetitions,
        require_clean_worktree=args.require_clean_worktree,
    )
    try:
        artifact = runner.run(task_ids=args.task_ids)
    except (RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    summary = artifact["summary"]
    print(
        f"overall: {summary['passed']}/{summary['attempt_count']} "
        f"({summary['pass_rate']:.1%})"
    )
    print(f"artifact: {runner.artifact_path}")
    print(f"report: {runner.report_path}")
    return 0 if summary["passed"] == summary["attempt_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
