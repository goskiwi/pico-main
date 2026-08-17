"""Evidence-aware progress governance for long-running agent loops."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProgressDecision:
    phase: str
    decision: str
    reason: str
    evidence_delta: int
    no_progress_steps: int
    repeated_failure_count: int
    strategy_revision: int
    failure_signature: str = ""
    evidence_fingerprint: str = ""
    steps_without_edit: int = 0

    def to_dict(self):
        return {
            "phase": self.phase,
            "decision": self.decision,
            "reason": self.reason,
            "evidence_delta": self.evidence_delta,
            "no_progress_steps": self.no_progress_steps,
            "repeated_failure_count": self.repeated_failure_count,
            "strategy_revision": self.strategy_revision,
            "failure_signature": self.failure_signature,
            "evidence_fingerprint": self.evidence_fingerprint,
            "steps_without_edit": self.steps_without_edit,
        }

    def guidance(self):
        if self.decision != "replan":
            return ""
        if self.reason == "six tool steps without workspace edit":
            return (
                "Runtime reflection: six tool steps completed without editing the workspace. "
                "If you have enough context, make the edit now. If not, identify exactly one "
                "missing fact, obtain it in one targeted step, then edit."
            )
        return (
            "Runtime progress governor: the current strategy is not producing new evidence "
            f"({self.reason}). State a different hypothesis and choose a materially different next action."
        )


@dataclass
class ProgressGovernor:
    phase: str = "exploring"
    no_progress_steps: int = 0
    strategy_revision: int = 0
    context_generation: int = 1
    seen_evidence: set[str] = field(default_factory=set)
    failure_counts: dict[str, int] = field(default_factory=dict)
    steps_without_edit: int = 0

    @classmethod
    def from_events(cls, events):
        governor = cls()
        for event in events:
            if event.get("event_type") == "context_folded":
                governor.context_generation = max(
                    governor.context_generation,
                    int(event.get("payload", {}).get("generation", 1)),
                )
                continue
            if event.get("event_type") != "progress_decided":
                continue
            payload = dict(event.get("payload", {}))
            governor.phase = str(payload.get("phase", governor.phase))
            governor.no_progress_steps = int(payload.get("no_progress_steps", 0))
            governor.strategy_revision = int(payload.get("strategy_revision", 0))
            governor.steps_without_edit = (
                0
                if payload.get("reason") == "six tool steps without workspace edit"
                else int(payload.get("steps_without_edit", 0))
            )
            signature = str(payload.get("failure_signature", ""))
            count = int(payload.get("repeated_failure_count", 0))
            if signature:
                governor.failure_counts[signature] = max(governor.failure_counts.get(signature, 0), count)
            evidence_fingerprint = str(payload.get("evidence_fingerprint", ""))
            if evidence_fingerprint and int(payload.get("evidence_delta", 0)):
                governor.seen_evidence.add(evidence_fingerprint)
        return governor

    @staticmethod
    def _failure_signature(outcome):
        failure = outcome.failure
        if failure is None:
            return ""
        value = (
            f"{outcome.tool_name}|{failure.code}|{failure.category}|"
            f"{failure.detail}|{outcome.call_fingerprint}"
        )
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def observe_context(self, generation):
        generation = int(generation)
        folded = generation > self.context_generation
        self.context_generation = max(self.context_generation, generation)
        return folded

    def observe_tool(self, outcome):
        signature = self._failure_signature(outcome)
        repeated = 0
        if signature:
            repeated = self.failure_counts.get(signature, 0) + 1
            self.failure_counts[signature] = repeated

        evidence_key = "|".join(
            (
                # Evidence identity follows what the agent actually learned, not
                # argument spelling. Equivalent calls cannot manufacture progress.
                outcome.tool_name,
                outcome.status,
                outcome.side_effect_state,
                outcome.workspace_fingerprint,
                hashlib.sha256(outcome.content.encode("utf-8")).hexdigest()
                if outcome.status == "ok"
                else "",
            )
        )
        evidence_delta = int(
            outcome.status == "ok"
            and (outcome.side_effect_state == "changed" or evidence_key not in self.seen_evidence)
        )
        workspace_changed = (
            outcome.side_effect_state == "changed"
            and outcome.metadata.get("effect_scope") in {"workspace", "mixed"}
        )
        if workspace_changed:
            self.steps_without_edit = 0
        elif outcome.execution_state != "not_started":
            self.steps_without_edit += 1
        reflected_no_edit_steps = self.steps_without_edit
        if self.steps_without_edit >= 6:
            self.steps_without_edit = 0
        if evidence_delta:
            self.seen_evidence.add(evidence_key)
            self.no_progress_steps = 0
        else:
            self.no_progress_steps += 1

        if outcome.side_effect_state in {"partial", "unknown"}:
            self.phase, decision, reason = "blocked", "repair", "uncertain workspace side effect"
        elif (
            outcome.side_effect_state == "changed"
            and outcome.metadata.get("effect_scope") == "project_memory"
        ):
            self.phase, decision, reason = "exploring", "continue", "durable project memory changed"
        elif outcome.side_effect_state == "changed":
            self.phase, decision, reason = "editing", "verify", "workspace changed"
        elif reflected_no_edit_steps >= 6:
            self.phase, decision, reason = "stalled", "replan", "six tool steps without workspace edit"
        elif repeated >= 2:
            self.phase, decision, reason = "stalled", "replan", "identical failure repeated"
        elif outcome.status == "ok" and not evidence_delta and self.no_progress_steps >= 2:
            self.phase, decision, reason = "stalled", "replan", "equivalent observation repeated"
        elif self.no_progress_steps >= 4:
            self.phase, decision, reason = "stalled", "replan", "no new evidence"
        elif outcome.status in {"error", "rejected"}:
            self.phase, decision, reason = "blocked", "repair", "tool did not complete"
        else:
            reason = "new evidence observed" if evidence_delta else "observation already seen"
            self.phase, decision = "exploring", "continue"

        if decision == "replan":
            self.strategy_revision += 1
        if self.no_progress_steps >= 8 and self.strategy_revision >= 2:
            decision, reason = "stop", "replanning did not restore progress"
        return ProgressDecision(
            self.phase,
            decision,
            reason,
            evidence_delta,
            self.no_progress_steps,
            repeated,
            self.strategy_revision,
            signature,
            evidence_key,
            reflected_no_edit_steps,
        )

    def observe_verification(self, record):
        status = str(record.get("status", "infrastructure_error"))
        signature = str(record.get("failure_signature", ""))
        repeated = 0
        if signature:
            repeated = self.failure_counts.get(signature, 0) + 1
            self.failure_counts[signature] = repeated
        if status == "passed":
            self.phase = "verifying"
            self.no_progress_steps = 0
            decision, reason, evidence_delta = "continue", "verification passed", 1
        elif repeated >= 2:
            self.phase = "stalled"
            self.strategy_revision += 1
            self.no_progress_steps += 1
            decision, reason, evidence_delta = "replan", "identical verification failure repeated", 0
        else:
            self.phase = "blocked"
            self.no_progress_steps += 1
            decision, reason, evidence_delta = "repair", "verification failed", 0
        return ProgressDecision(
            self.phase,
            decision,
            reason,
            evidence_delta,
            self.no_progress_steps,
            repeated,
            self.strategy_revision,
            signature,
            "",
        )

    def observe_model_retry(self, error):
        signature = hashlib.sha256(f"model|{error}".encode()).hexdigest()
        repeated = self.failure_counts.get(signature, 0) + 1
        self.failure_counts[signature] = repeated
        self.no_progress_steps += 1
        self.phase = "stalled" if repeated >= 2 else "blocked"
        decision = "replan" if repeated >= 2 else "repair"
        reason = "malformed model action repeated" if repeated >= 2 else "malformed model action"
        if decision == "replan":
            self.strategy_revision += 1
        if self.no_progress_steps >= 8 and self.strategy_revision >= 2:
            decision, reason = "stop", "model retries did not restore progress"
        return ProgressDecision(
            self.phase,
            decision,
            reason,
            0,
            self.no_progress_steps,
            repeated,
            self.strategy_revision,
            signature,
            "",
        )
