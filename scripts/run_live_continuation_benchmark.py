#!/usr/bin/env python3
"""Run Pico's frozen multi-turn working-memory and resume benchmark."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pico.evaluation.continuation_benchmark import (  # noqa: E402
    LiveContinuationBenchmarkRunner,
)
from pico.evaluation.continuation_benchmark_contract import (  # noqa: E402
    DEFAULT_CONTINUATION_ARTIFACT_PATH,
    DEFAULT_CONTINUATION_BENCHMARK_PATH,
    DEFAULT_CONTINUATION_REPORT_PATH,
    DEFAULT_CONTINUATION_WORKSPACE_ROOT,
)
from pico.sandbox import DockerSandboxConfig  # noqa: E402


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Run Pico's live two-phase memory and checkpoint-resume benchmark "
            "with hidden Docker verification."
        )
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model override. Defaults to OPENAI_MODEL from the benchmark env file.",
    )
    parser.add_argument("--base-url", default=None)
    parser.add_argument(
        "--env-file",
        default=None,
        help=(
            "Optional external .env.local for benchmark credentials. Its path and "
            "values are not recorded in result artifacts."
        ),
    )
    parser.add_argument(
        "--benchmark-path", default=str(DEFAULT_CONTINUATION_BENCHMARK_PATH)
    )
    parser.add_argument(
        "--artifact-path", default=str(DEFAULT_CONTINUATION_ARTIFACT_PATH)
    )
    parser.add_argument(
        "--report-path", default=str(DEFAULT_CONTINUATION_REPORT_PATH)
    )
    parser.add_argument(
        "--workspace-root", default=str(DEFAULT_CONTINUATION_WORKSPACE_ROOT)
    )
    parser.add_argument("--task", action="append", dest="task_ids")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--max-new-tokens", type=int, default=512)
    parser.add_argument("--verifier-timeout", type=int, default=90)
    parser.add_argument(
        "--require-clean-worktree",
        action="store_true",
        help="Refuse to run when tracked or untracked files are present.",
    )
    parser.add_argument("--sandbox-image", default="pico/sandbox:latest")
    parser.add_argument("--sandbox-cpus", type=float, default=4.0)
    parser.add_argument("--sandbox-memory", default="4g")
    parser.add_argument("--sandbox-pids-limit", type=int, default=512)
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    runner = LiveContinuationBenchmarkRunner(
        benchmark_path=args.benchmark_path,
        artifact_path=args.artifact_path,
        report_path=args.report_path,
        workspace_root=args.workspace_root,
        model=args.model,
        base_url=args.base_url,
        env_file=args.env_file,
        repetitions=args.repetitions,
        max_new_tokens=args.max_new_tokens,
        verifier_timeout=args.verifier_timeout,
        require_clean_worktree=args.require_clean_worktree,
        sandbox_config=DockerSandboxConfig(
            image=args.sandbox_image,
            cpus=args.sandbox_cpus,
            memory=args.sandbox_memory,
            pids_limit=args.sandbox_pids_limit,
        ),
    )
    artifact = runner.run(task_ids=args.task_ids)
    summary = artifact["summary"]
    print(
        f"overall: {summary['passed']}/{summary['episode_count']} episodes "
        f"({summary['pass_rate']:.1%})"
    )
    memory_pairs = summary["memory"]["paired_read_comparison"]
    print(
        "memory phase-2 successful source-read delta "
        f"(working-memory - memory-disabled): "
        f"{memory_pairs['successful_source_read_delta']}"
    )
    resume = summary["resume"]
    print(
        f"resume: {resume['passed']}/{resume['attempt_count']} recoveries; "
        f"{resume['status_matches']}/{resume['attempt_count']} status matches"
    )
    print(f"artifact: {runner.artifact_path}")
    print(f"report: {runner.report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
