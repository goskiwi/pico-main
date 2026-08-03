#!/usr/bin/env python3
"""Hidden verifier installed only after a continuation episode completes."""

from __future__ import annotations

import argparse
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-path", required=True)
    parser.add_argument("--expected-output", required=True)
    args = parser.parse_args(argv)

    output = Path(args.output_path)
    if not output.is_file():
        raise SystemExit(f"missing output: {args.output_path}")
    actual = output.read_text(encoding="utf-8")
    expected = args.expected_output + "\n"
    if actual != expected:
        raise SystemExit(
            f"unexpected output in {args.output_path}: expected one exact line"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
