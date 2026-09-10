"""Configured verification shared by tool execution and final acceptance."""

import json
import uuid
from dataclasses import dataclass, field

from .contracts import ToolExecutionPlan
from .execution import ExecutionCancelled, ExecutionDeadlineExceeded
from .mutations import file_revision
from .verification import (
    capture_repository_state,
    repository_state_changes,
    verify_workspace,
)


@dataclass(frozen=True)
class VerificationResult:
    status: str
    detail: str
    error: str = ""
    data: dict = field(default_factory=dict)

    @property
    def allowed(self):
        return self.status == "success"


class VerificationService:
    def __init__(self, runtime):
        self.runtime = runtime

    def drift(self, execution):
        root = self.runtime.workspace.root
        changed = []
        for path, expected in self.runtime.session.file_states.items():
            target = root / path
            execution.check_active()
            if target.resolve() != target or file_revision(target, execution_context=execution) != expected:
                changed.append(path)
        return changed

    def inspect(self, execution):
        session = self.runtime.session
        unchecked = [item for item in session.unconfirmed if not item["observed"]]
        if unchecked:
            return VerificationResult("continue", "Inspect interrupted effects; reread affected files. "
                                      "Do not replay old writes blindly.", "effect_observation_required",
                                      {"unconfirmed": unchecked})
        drift = self.drift(execution)
        if drift:
            return VerificationResult("continue", "Files changed since the accepted observation. "
                                      "Reread and preserve external edits: " + ", ".join(drift),
                                      "workspace_drift", {"changed_paths": drift})
        return None

    def run(self, execution, *, tool_call=False):
        blocked = self.inspect(execution)
        if blocked:
            return blocked
        command = self.runtime.config.verification_command.strip()
        if not command or self.runtime.config.mode == "ask":
            return VerificationResult("stop", "This task still requires a configured verifier.",
                                      "verification_required")
        if not self._approve(command):
            return VerificationResult("stop", "Verification was not approved.", "verification_denied")
        if (self.runtime.config.mode == "ask"
                or command != self.runtime.config.verification_command.strip()
                or (tool_call and "verify" not in self.runtime.tools.resolve_surface().names)):
            return VerificationResult("stop", "Verification permissions changed during approval.",
                                      "permission_changed")
        drift = self.drift(execution)
        if drift:
            return VerificationResult("continue", "Files changed during approval; reread them.",
                                      "workspace_drift", {"changed_paths": drift})
        return self._verify(command, execution, record_feedback=not tool_call)

    def _verify(self, command, execution, *, record_feedback):
        session = self.runtime.session
        operation = "verify:" + uuid.uuid4().hex[:12]
        session.verification = {"status": "running", "command": command,
                                "operation_id": operation, "workspace_state": None}
        session.save()
        observed = []
        record = verify_workspace(
            root=self.runtime.workspace.root, command=command,
            command_runner=self.runtime.command_runner,
            timeout_seconds=self.runtime.config.turn_timeout_seconds,
            redact_text=self.runtime.redact_text,
            mutation_sequence_provider=lambda: len(session.mutations),
            started_workspace_mutation_sequence=len(session.mutations),
            changed_paths=sorted(session.file_states), execution_context=execution,
            observed_state=observed.append,
        )
        if len(record["output"]) > 4000:
            artifact = self.runtime.artifacts.write_tool_output(
                session.id, operation, json.dumps(record, ensure_ascii=False))
            record["artifact_id"] = artifact["artifact_id"]
            record["output"] = (record["output"][:2000]
                                + "\n[Middle omitted; use read_artifact: "
                                + artifact["artifact_id"] + "]\n" + record["output"][-2000:])
        passed = record["status"] == "passed"
        session.verification.update(status="passed" if passed else "failed",
                                    workspace_state=record["finished_changed_path_states"] if passed else None)
        if record["workspace_changes"] is None:
            session.add_unconfirmed(operation, "verify")
        if record_feedback:
            session.append_feedback("Runtime verification: " + json.dumps(record, ensure_ascii=False))
        session.save()
        self.runtime.emit_trace("verification", status=record["status"], exit_code=record["exit_code"])
        execution.check_active()
        if not passed:
            return VerificationResult("continue", "Verification failed. Inspect output and repair.",
                                      "verification_failed", record)
        try:
            current = capture_repository_state(self.runtime.workspace.root,
                                               command_runner=self.runtime.command_runner,
                                               execution_context=execution)
            changed = list(repository_state_changes(observed[-1], current)) + self.drift(execution)
        except (ExecutionCancelled, ExecutionDeadlineExceeded):
            raise
        except (OSError, RuntimeError) as exc:
            session.verification.update(status="stale", workspace_state=None)
            session.add_unconfirmed(operation, "verify")
            session.save()
            return VerificationResult("continue", str(exc), "verification_unconfirmed")
        if changed:
            session.verification.update(status="stale", workspace_state=None)
            session.save()
            return VerificationResult("continue", "Workspace changed after verification; inspect and retry.",
                                      "verification_stale", {"changed_paths": sorted(set(changed))})
        return VerificationResult("success", "Configured verification passed.", data=record)

    def _approve(self, command):
        if self.runtime.config.mode == "auto":
            return True
        handler = self.runtime.approval_handler
        return bool(
            handler
            and handler(
                "verify",
                {"command": command},
                ToolExecutionPlan("workspace", operation={"command": command}),
            )
        )
