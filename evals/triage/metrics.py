"""Metrics for fixed Pico Triage cases."""

from __future__ import annotations


def summarize_triage_rows(rows):
    rows = list(rows)
    total = len(rows)

    def rate(field):
        return sum(bool(row[field]) for row in rows) / total if total else 0.0

    return {
        "total_cases": total,
        "reproduction_rate": rate("reproduced"),
        "root_cause_top1_rate": rate("root_cause_top1"),
        "patch_success_rate": rate("patch_correct"),
        "verification_pass_rate": rate("verification_passed"),
        "within_budget_rate": rate("within_budget"),
    }
