#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.real_benchmark import (  # noqa: E402
    compare_real_benchmark_artifacts,
    render_real_benchmark_comparison_markdown,
)


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Compare matched text-protocol and structured-action benchmark artifacts."
    )
    parser.add_argument("baseline")
    parser.add_argument("candidate")
    parser.add_argument("--artifact-path", default="artifacts/structured-action-comparison.json")
    parser.add_argument("--report-path", default="docs/metrics/structured-action-comparison.md")
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    comparison = compare_real_benchmark_artifacts(args.baseline, args.candidate)
    artifact_path = Path(args.artifact_path)
    report_path = Path(args.report_path)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(comparison, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report_path.write_text(
        render_real_benchmark_comparison_markdown(comparison),
        encoding="utf-8",
    )
    summary = comparison["summary"]
    print(
        f"pass rate: {summary['baseline_pass_rate']:.1%} -> "
        f"{summary['candidate_pass_rate']:.1%} ({summary['pass_rate_delta']:+.1%})"
    )
    print(
        f"avg model calls: {summary['baseline_avg_model_calls']:.2f} -> "
        f"{summary['candidate_avg_model_calls']:.2f} ({summary['avg_model_calls_delta']:+.2f})"
    )
    print(f"artifact: {artifact_path}")
    print(f"report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
