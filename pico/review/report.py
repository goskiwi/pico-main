"""Merge and render structured PR-review reports."""

from .contracts import ReviewReport


def merge_review_reports(reports):
    reports = tuple(reports)
    if not reports:
        raise ValueError("at least one review report is required")
    first = reports[0]
    identity = (first.review_id, first.policy_version, first.policy_digest)
    if any(
        (report.review_id, report.policy_version, report.policy_digest) != identity
        for report in reports[1:]
    ):
        raise ValueError("review report identities do not match")
    findings = {}
    run_ids = []
    for report in reports:
        for run_id in report.run_ids:
            if run_id not in run_ids:
                run_ids.append(run_id)
        for finding in report.findings:
            findings.setdefault(finding.finding_id, finding)
    merged_findings = list(findings.values())
    return ReviewReport(
        review_id=first.review_id,
        run_ids=run_ids,
        policy_version=first.policy_version,
        policy_digest=first.policy_digest,
        verdict="findings" if merged_findings else "clean",
        summary=(
            f"Reviewed {len(reports)} diff chunk(s); "
            f"reported {len(merged_findings)} finding(s)."
        ),
        findings=merged_findings,
    )


def render_review_markdown(report):
    report = report if isinstance(report, ReviewReport) else ReviewReport.model_validate(report)
    lines = [
        "# Pico PR Review",
        "",
        f"- Verdict: **{report.verdict.upper()}**",
        f"- Findings: **{len(report.findings)}**",
        f"- Review ID: `{report.review_id}`",
        f"- Policy: `{report.policy_version}`",
        f"- Runs: {', '.join(f'`{run_id}`' for run_id in report.run_ids) or 'none'}",
        "",
        report.summary,
    ]
    for index, finding in enumerate(report.findings, start=1):
        location = (
            f"{finding.path}:{finding.start_line}"
            if finding.start_line == finding.end_line
            else f"{finding.path}:{finding.start_line}-{finding.end_line}"
        )
        lines.extend(
            [
                "",
                f"## {index}. {finding.title}",
                "",
                f"- Severity: `{finding.severity}`",
                f"- Category: `{finding.category}`",
                f"- Confidence: `{finding.confidence:.2f}`",
                f"- Location: `{location}`",
                f"- CWE: `{finding.cwe or 'unknown'}`",
                "",
                finding.explanation,
                "",
                f"Evidence: {finding.evidence}",
            ]
        )
        if finding.suggested_fix:
            lines.extend(["", f"Suggested fix: {finding.suggested_fix}"])
    return "\n".join(lines).rstrip() + "\n"


def render_review_json(report):
    report = report if isinstance(report, ReviewReport) else ReviewReport.model_validate(report)
    return report.model_dump_json(indent=2) + "\n"
