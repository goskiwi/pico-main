"""Parse pytest output for evaluation reports."""

from __future__ import annotations

import re

PYTEST_COUNT_RE = re.compile(
    r"(?P<count>\d+)\s+(?P<kind>passed|failed|errors?|skipped|xfailed|xpassed)"
)
PYTEST_FAILED_RE = re.compile(r"^FAILED\s+([^\s]+)", re.MULTILINE)
PYTEST_COLLECTED_RE = re.compile(r"collected\s+(\d+)\s+items?", re.IGNORECASE)
PYTEST_PROGRESS_RE = re.compile(r"^([.FEsxX]+)\s+\[\s*\d+%\]$", re.MULTILINE)


def parse_pytest_output(output):
    output = str(output)
    collected = PYTEST_COLLECTED_RE.search(output)
    details = {
        "collected": int(collected.group(1)) if collected else None,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "failed_tests": sorted(set(PYTEST_FAILED_RE.findall(output)))[:50],
    }
    for match in PYTEST_COUNT_RE.finditer(output):
        kind = match.group("kind")
        kind = "errors" if kind in {"error", "errors"} else kind
        if kind in details:
            details[kind] = max(details[kind], int(match.group("count")))
    progress = "".join(PYTEST_PROGRESS_RE.findall(output))
    if progress:
        details["collected"] = details["collected"] or len(progress)
        progress_counts = {
            "passed": progress.count(".") + progress.count("X"),
            "failed": progress.count("F"),
            "errors": progress.count("E"),
            "skipped": progress.count("s") + progress.count("x"),
        }
        for kind, count in progress_counts.items():
            details[kind] = max(details[kind], count)
    return details
