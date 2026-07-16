"""Small utilities shared by offline evaluation modules."""

import subprocess
from datetime import datetime, timezone
from pathlib import Path


def safe_mean(values):
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def safe_ratio(numerator, denominator):
    return numerator / denominator if denominator else 0.0


def utc_timestamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def git_value(args, *, cwd=None, fallback=""):
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or Path.cwd(),
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return result.stdout.strip() or fallback
    except (OSError, subprocess.SubprocessError):
        return fallback
