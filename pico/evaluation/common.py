"""Small utilities shared by evaluation modules."""

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


def git_value(args, *, cwd=None, fallback="", preserve_empty=False):
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or Path.cwd(),
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        output = result.stdout.strip()
        return output if output or preserve_empty else fallback
    except (OSError, subprocess.SubprocessError):
        return fallback
