#!/usr/bin/env python3
"""Run the fixed end-to-end Pico Triage cases."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.triage import run_triage_evaluation


def main():
    artifact = run_triage_evaluation()
    summary = artifact["summary"]
    passed = all(
        value == 1.0
        for key, value in summary.items()
        if key.endswith("_rate")
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
