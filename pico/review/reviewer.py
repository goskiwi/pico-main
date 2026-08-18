"""Single-agent, read-only PR-review adapter."""

import hashlib

from .contracts import Finding, ReviewReport, ReviewRequest
from .diff import parse_added_lines
from .prompt import REVIEW_POLICY_VERSION, build_review_prompt, review_policy_digest

REVIEW_ALLOWED_TOOLS = frozenset(
    {"list_files", "read_file", "read_artifact", "search", "query_repo_map"}
)


def _json_text(answer):
    text = str(answer or "").strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()
    return text


def _finding_id(finding):
    identity = "\0".join(
        (
            finding.category,
            finding.path,
            str(finding.start_line),
            str(finding.end_line),
            finding.title,
        )
    )
    return "finding_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


class PRReviewer:
    def __init__(self, agent):
        allowed = set(agent.allowed_tools or ())
        if not agent.read_only:
            raise ValueError("PRReviewer requires a read-only Pico Runtime")
        if not allowed or allowed - REVIEW_ALLOWED_TOOLS:
            raise ValueError("PRReviewer requires an explicit read-only tool surface")
        self.agent = agent

    def review(self, request):
        request = request if isinstance(request, ReviewRequest) else ReviewRequest.model_validate(request)
        answer = self.agent.ask(build_review_prompt(request))
        report = ReviewReport.model_validate_json(_json_text(answer))
        changed_files = set(request.changed_files)
        changed_lines = parse_added_lines(request.diff)
        findings = []
        for finding in report.findings:
            if finding.path not in changed_files:
                raise ValueError("review findings must point to a changed file")
            allowed_lines = changed_lines.get(finding.path, frozenset())
            finding_lines = set(range(finding.start_line, finding.end_line + 1))
            if not finding_lines or not finding_lines.issubset(allowed_lines):
                raise ValueError("review findings must point to added diff lines")
            findings.append(
                Finding.model_validate(
                    {**finding.model_dump(), "finding_id": _finding_id(finding)}
                )
            )
        policy_digest = review_policy_digest()
        review_identity = (
            f"{request.repository}\0{request.base_sha}\0{request.head_sha}\0{policy_digest}"
        )
        return report.model_copy(
            update={
                "review_id": "review_" + hashlib.sha256(
                    review_identity.encode("utf-8")
                ).hexdigest()[:16],
                "run_ids": [self.agent.current_task_state.run_id],
                "policy_version": REVIEW_POLICY_VERSION,
                "policy_digest": policy_digest,
                "findings": findings,
            }
        )
