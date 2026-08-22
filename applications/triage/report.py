"""Evidence-linked business report derived from one Pico Run."""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pico.evidence import RunEvidence
from pico.run_log import replay_events
from pico.workspace import clip

from .case import TriageCase


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RootCause(StrictModel):
    summary: str = Field(min_length=1, max_length=4000)
    files: tuple[str, ...] = Field(min_length=1, max_length=20)


class EvidenceItem(StrictModel):
    kind: Literal[
        "test_result",
        "stack_trace",
        "source",
        "git_history",
        "coverage",
        "other",
    ]
    claim: str = Field(min_length=1, max_length=2000)
    tool_call_id: str = Field(min_length=1)
    path: str = ""
    line: int | None = Field(default=None, ge=1)


class ModelDiagnosis(StrictModel):
    status: Literal["fixed", "diagnosed", "blocked"]
    root_cause: RootCause
    evidence: tuple[EvidenceItem, ...] = Field(min_length=1, max_length=30)


class Reproduction(StrictModel):
    command: str
    status: Literal["reproduced", "passed", "blocked", "not_run"]
    tool_call_id: str = ""
    output: str = ""


class PatchResult(StrictModel):
    changed_paths: tuple[str, ...] = ()


class VerificationResult(StrictModel):
    command: str = ""
    status: str = "not_run"
    output: str = ""


class TriageReport(StrictModel):
    schema_version: Literal["pico-triage-report"] = "pico-triage-report"
    incident_id: str
    repository_revision: str
    run_id: str
    status: Literal["fixed", "diagnosed", "blocked"]
    root_cause: RootCause
    evidence: tuple[EvidenceItem, ...]
    reproduction: Reproduction
    patch: PatchResult
    verification: VerificationResult
    executed_tool_count: int = Field(ge=0)


def _parse_diagnosis(answer) -> ModelDiagnosis:
    text = str(answer).strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("Triage final answer must be one JSON object") from exc
    return ModelDiagnosis.model_validate(value)


def _reproduction(case, calls, results):
    call = next(
        (
            entry
            for entry in calls.values()
            if entry.name == "run_shell"
            and str(entry.args.get("command", "")).strip() == case.failing_command
        ),
        None,
    )
    if call is None:
        return Reproduction(command=case.failing_command, status="not_run")
    result = results.get(call.call_id)
    if result is None or result.outcome_status == "rejected":
        status = "blocked"
    elif result.outcome_status in {"error", "partial_success"}:
        status = "reproduced"
    else:
        status = "passed"
    return Reproduction(
        command=case.failing_command,
        status=status,
        tool_call_id=call.call_id,
        output=clip(result.content if result is not None else "", 2000),
    )


def _resolve_evidence(items, ordered_calls, results):
    completed_call_ids = {
        entry.call_id for entry in ordered_calls if entry.call_id in results
    }
    resolved = []
    unknown = []
    for item in items:
        call_id = item.tool_call_id
        if call_id not in completed_call_ids:
            ordinal = re.fullmatch(r"call_(\d+)", call_id)
            index = int(ordinal.group(1)) - 1 if ordinal else -1
            if not 0 <= index < len(ordered_calls):
                unknown.append(call_id)
                continue
            candidate = ordered_calls[index].call_id
            if candidate not in completed_call_ids:
                unknown.append(call_id)
                continue
            call_id = candidate
        resolved.append(item.model_copy(update={"tool_call_id": call_id}))
    if unknown:
        raise ValueError(
            "Triage evidence references unknown Tool Calls: "
            + ", ".join(sorted(set(unknown)))
        )
    return tuple(resolved)


def build_triage_report(case: TriageCase, answer, events) -> TriageReport:
    events = tuple(events)
    diagnosis = _parse_diagnosis(answer)
    ordered_calls = [
        entry for entry in events if entry.kind == "assistant_tool_call"
    ]
    calls = {
        entry.call_id: entry for entry in ordered_calls
    }
    results = {
        entry.call_id: entry for entry in events if entry.kind == "tool_result"
    }
    evidence_items = _resolve_evidence(
        diagnosis.evidence,
        ordered_calls,
        results,
    )

    run_evidence = RunEvidence.from_events(events)
    verification_event = next(
        (entry for entry in reversed(events) if entry.kind == "verification_result"),
        None,
    )
    verification_payload = (
        dict(verification_event.payload) if verification_event is not None else {}
    )
    verification = VerificationResult(
        command=str(verification_payload.get("command", "")),
        status=str(verification_payload.get("status", "not_run")),
        output=clip(verification_payload.get("output", ""), 2000),
    )
    changed_paths = tuple(run_evidence.changed_paths)
    reproduction = _reproduction(case, calls, results)
    if diagnosis.status == "fixed" and (
        reproduction.status != "reproduced"
        or not changed_paths
        or verification.status != "passed"
    ):
        raise ValueError(
            "A fixed Triage report requires reproduction, a patch, and passed verification"
        )

    projection = replay_events(events)
    return TriageReport(
        incident_id=case.incident_id,
        repository_revision=case.revision,
        run_id=projection.run_id,
        status=diagnosis.status,
        root_cause=diagnosis.root_cause,
        evidence=evidence_items,
        reproduction=reproduction,
        patch=PatchResult(changed_paths=changed_paths),
        verification=verification,
        executed_tool_count=projection.executed_tool_count,
    )
