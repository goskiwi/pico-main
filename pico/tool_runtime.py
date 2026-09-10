"""The single execution boundary for model-visible tools.

ToolRuntime validates and authorizes a call, persists its phase, invokes the
small runner from :mod:`pico.tools`, and records the observed result directly in
the Session snapshot. No event projection is involved.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from pydantic import ValidationError

from .contracts import (
    TOOL_OUTPUT_MAX_BYTES,
    FailureInfo,
    ToolExecutionPlan,
    ToolFailureError,
    ToolOutcome,
    ToolRunnerResult,
)
from .execution import ExecutionCancelled, ExecutionDeadlineExceeded
from .mutations import content_revision, file_revision
from .tool_context import ToolContext
from .tools import ToolArgs, build_action_tools, build_tool_registry
from .verification_service import VerificationService

READ_TOOLS = frozenset({"list_files", "read_file", "read_artifact", "search"})
WRITE_TOOLS = frozenset({"write_file", "edit_file"})


@dataclass(frozen=True)
class ResolvedToolSurface:
    registry: dict
    action_tools: tuple[dict, ...]

    @property
    def names(self):
        return frozenset(self.registry)


class ToolRuntime:
    def __init__(self, runtime):
        self.runtime = runtime
        self.read_versions: dict[str, str] = {}
        self.registry = build_tool_registry(
            workspace_root=runtime.workspace.root,
            path_resolver=runtime.workspace.resolve_tool_path,
            read_path_resolver=self._resolve_read_path,
            artifact_store=runtime.artifacts,
            redact_text=runtime.redact_text,
            mutation_service=runtime.mutations,
            command_runner=runtime.command_runner,
        )
        self.registry["verify"] = {
            "args_schema": ToolArgs,
            "description": "Run the configured acceptance checks now and return failures for repair. "
                           "Use after meaningful edits, before submit_final. No command arguments; "
                           "this does not declare the task complete.",
            "plan": lambda context, args: ToolExecutionPlan(
                "workspace", operation={"command": self.runtime.config.verification_command}),
            "validate": lambda context, args: None,
            "run": self._run_verification,
        }
        if hasattr(runtime.model_client.client, "new_isolated_client"):
            from .delegate import definition
            self.registry["delegate"] = definition(runtime)

    def resolve_surface(self):
        mode = self.runtime.config.mode
        names = set(READ_TOOLS)
        if mode == "code":
            names.update({"run_shell", "delegate", *WRITE_TOOLS})
        elif mode == "auto":
            names.update({"delegate", *WRITE_TOOLS})
        if self.effective_paths() is not None:
            names.discard("run_shell")
        if self.effective_paths() == ():
            names.difference_update(WRITE_TOOLS)
        if mode != "ask" and self.runtime.config.verification_command.strip():
            names.add("verify")
        allowed = self.runtime.config.allowed_tools
        if allowed is not None:
            names.intersection_update(allowed)
        registry = {name: self.registry[name] for name in self.registry if name in names}
        return ResolvedToolSurface(registry, tuple(build_action_tools(registry)))

    def _resolve_read_path(self, raw_path):
        path = Path(raw_path)
        path = path if path.is_absolute() else self.runtime.workspace.root / path
        transcript = self.runtime.session.store.transcript_path(self.runtime.session.id)
        if path == transcript and path.resolve() == transcript:
            return transcript
        return self.runtime.workspace.resolve_tool_path(raw_path)

    def execute_group(self, calls, entry, execution_context, surface):
        """Execute in model order; every phase transition is persisted."""
        outcomes = []
        for call in calls:
            execution_context.check_active()
            outcomes.append(self._execute(call, entry, execution_context, surface))
        return tuple(outcomes)

    def reject_group(self, entry, calls):
        for call in calls:
            self._finish_rejected(entry, call, "repository_instructions_changed",
                                  "Directory rules changed. Read the updated instructions and decide again.")

    def effective_paths(self):
        current = self.runtime.config.allowed_write_paths
        saved = self.runtime.session.task_policy.get("write_paths")
        if self.runtime.config.mode == "ask":
            return ()
        if current is None:
            return None if saved is None else tuple(saved)
        return tuple(current) if saved is None else tuple(p for p in current if p in saved)

    def _execute(self, call, entry, execution_context, surface):
        session = self.runtime.session
        tool = surface.registry.get(call.name)
        if tool is None:
            return self._finish_rejected(
                entry, call, "unknown_tool", f"tool is unavailable: {call.name}"
            )
        try:
            args = tool["args_schema"].model_validate(call.args).model_dump()
            context = ToolContext(
                run_id=session.id,
                tool_call_id=call.call_id,
                execution_context=execution_context,
                execution_plan=ToolExecutionPlan("none"),
            )
            planner = tool.get("plan")
            if planner is not None:
                context.execution_plan = planner(context, args)
            self._check_write_scope(context.execution_plan)
            expected_revision = self._observed_revision(call.name, context.execution_plan)
            approval = self._approve(call, args, tool, context.execution_plan)
            if approval is not None:
                return self._finish(entry, call, approval)
            if call.name not in self.resolve_surface().names:
                raise ToolFailureError("permission_changed", "Tool permission changed during approval")
            self._check_write_scope(context.execution_plan)
            self._recheck_targets(call.name, args, context.execution_plan)
            tool["validate"](context, args)
        except ValidationError as exc:
            return self._finish_rejected(
                entry,
                call,
                "invalid_arguments",
                "arguments do not satisfy the tool schema",
                structured={"issues": exc.errors(include_input=False, include_url=False)},
            )
        except ToolFailureError as exc:
            return self._finish_rejected(
                entry,
                call,
                exc.failure.code,
                exc.failure.detail,
                recovery=exc.failure.recovery,
                structured=exc.structured,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            return self._finish_rejected(
                entry, call, "admission_failed", str(exc), recovery="retry_after_change"
            )

        session.start_tool(entry, call.call_id)
        history_index = len(session.history) - 1
        self.operation_id = f"{history_index}:{call.call_id}"
        entry.setdefault("plans", {})[call.call_id] = {
            "effect_scope": context.execution_plan.effect_scope,
            "paths": [logical for logical, _target in context.execution_plan.paths],
            "operation": self.runtime.redact_facts(context.execution_plan.operation),
        }
        session.save()
        self.runtime.emit_trace("tool_started", tool=call.name, call_id=call.call_id)
        try:
            runner_result, preimage_id = self._invoke(
                call.name, call.call_id, tool, context, args, expected_revision
            )
            outcome = self._outcome(call, context.execution_plan, runner_result)
            if call.name in {"read_file", "search"}:
                target = self._resolve_read_path(args.get("path", "."))
                if target == session.store.transcript_path(session.id):
                    outcome.structured["historical"] = True
            if call.name in WRITE_TOOLS:
                outcome = self._record_mutation(call, outcome, preimage_id)
        except (ExecutionCancelled, ExecutionDeadlineExceeded):
            # Leave running operations for Session recovery; do not manufacture
            # unknown effects for reads or continue siblings after cancellation.
            raise
        except ToolFailureError as exc:
            outcome = self._error_outcome(
                call,
                context.execution_plan,
                exc.failure.code,
                exc.failure.detail,
                recovery=exc.failure.recovery,
            )
        except Exception as exc:  # noqa: BLE001 - execution boundary preserves uncertainty
            outcome = self._error_outcome(
                call,
                context.execution_plan,
                "execution_failed",
                f"{type(exc).__name__}: {exc}",
                unknown=context.execution_plan.effect_scope != "none",
            )
        return self._finish(entry, call, outcome)

    def _invoke(self, name, call_id, tool, context, args, expected_revision):
        preimage_id = ""
        if name == "write_file":
            logical, _target = context.execution_plan.paths[0]
            self._prepare_mutation(call_id, name, logical, "absent", "",
                                   content_revision(args["content"].encode()))
            return tool["run"](context, args), preimage_id
        if name != "edit_file":
            return tool["run"](context, args), preimage_id
        _logical, target = context.execution_plan.paths[0]
        with self.runtime.mutations.prepare_edit(
            target,
            expected_revision,
            execution_context=context.execution_context,
        ) as original:
            descriptor = self.runtime.artifacts.write_workspace_preimage(
                self.runtime.session.id,
                self.operation_id,
                target.relative_to(self.runtime.workspace.root).as_posix(),
                BytesIO(original[0]),
            )
            preimage_id = descriptor["artifact_id"]
            self._prepare_mutation(
                call_id, name, target.relative_to(self.runtime.workspace.root).as_posix(),
                expected_revision, preimage_id,
            )
            return tool["run"](context, args, original=original[0],
                               expected_revision=expected_revision,
                               before_commit=self._planned_after), preimage_id

    def _run_verification(self, context, args):
        result = VerificationService(self.runtime).run(context.execution_context, tool_call=True)
        data = dict(result.data)
        output = data.pop("output", "")
        changes = data.get("workspace_changes", [])
        return ToolRunnerResult(
            content=result.detail + ("\n" + output if output else ""),
            structured={"status": result.status, **data},
            failure=None if result.allowed else FailureInfo(
                result.error, result.detail, "retry_after_change"),
            affected_paths=tuple(changes or ()),
            effect_scope="workspace" if changes is None or changes else "none",
        )

    def _planned_after(self, revision):
        self.runtime.session.mutations[-1]["after_revision"] = revision
        self.runtime.session.save()

    def _prepare_mutation(self, call_id, tool, path, before_revision, preimage_id, after_revision=""):
        receipt = {
            "id": self.operation_id,
            "tool": tool,
            "path": path,
            "before_revision": before_revision,
            "after_revision": after_revision,
            "preimage_id": preimage_id,
            "status": "prepared",
        }
        self.runtime.session.mutations.append(receipt)
        self.runtime.session.save()

    def _outcome(self, call, plan, result):
        structured = dict(result.structured)
        affected = tuple(result.affected_paths)
        effect_scope = result.effect_scope
        if call.name in WRITE_TOOLS:
            before = structured.get("before_revision")
            after = structured.get("after_revision")
            path = structured.get("path")
            affected = (path,) if path and before != after else ()
            effect_scope = "workspace" if affected else "none"
            target = dict(plan.paths)[path]
            actual = file_revision(target) if target.resolve() == target else "redirected"
            if actual != after:
                return self._error_outcome(call, plan, "post_write_drift",
                                           "File changed after publication; reread before continuing", unknown=True)
        elif call.name == "run_shell":
            affected = tuple(structured.get("repository_changes", ()))
        failure = result.failure
        unknown = failure is not None and effect_scope != "none" and not affected
        status = (
            "success"
            if failure is None
            else ("partial_success" if affected or unknown else "error")
        )
        effect = (
            "partial"
            if failure is not None and affected
            else ("unknown" if unknown else ("changed" if affected else "none"))
        )
        return ToolOutcome(
            call.call_id,
            call.name,
            status,
            "completed" if failure is None else "failed",
            effect,
            str(result.content),
            structured=structured,
            failure=failure,
            affected_paths=affected,
            effect_scope=effect_scope,
        )

    def _record_mutation(self, call, outcome, preimage_id):
        data = outcome.structured
        receipt = next(
            item for item in reversed(self.runtime.session.mutations)
            if item["id"] == self.operation_id
        )
        receipt.update(
            path=data.get("path", receipt["path"]),
            before_revision=data.get("before_revision", receipt["before_revision"]),
            after_revision=data.get("after_revision", receipt["after_revision"]),
            preimage_id=preimage_id or receipt["preimage_id"],
            status=(
                "unknown" if outcome.side_effect_state == "unknown" else
                ("applied" if outcome.side_effect_state == "changed" else "not_applied")
            ),
        )
        if outcome.status == "success" and receipt["path"] and receipt["after_revision"]:
            self.read_versions[receipt["path"]] = receipt["after_revision"]
            if receipt["status"] == "applied":
                self.runtime.session.file_states[receipt["path"]] = receipt["after_revision"]
        return outcome

    def _finish(self, entry, call, outcome):
        session = self.runtime.session
        outcome = ToolOutcome.from_dict(self.runtime.redact_facts(outcome.to_dict()))
        if not outcome.structured.get("historical"):
            self._observe(outcome)
        serialized = json.dumps(outcome.to_dict(), ensure_ascii=False)
        if len(serialized.encode("utf-8")) > TOOL_OUTPUT_MAX_BYTES or len(outcome.content) > 8000:
            descriptor = self.runtime.artifacts.write_tool_output(
                session.id,
                f"{len(session.history) - 1}:{call.call_id}",
                serialized,
            )
            outcome = ToolOutcome(
                **{
                    **outcome.__dict__,
                    "artifact_id": descriptor["artifact_id"],
                }
            )
        session.finish_tool(entry, call.call_id, outcome.to_dict())
        session.save()
        self.runtime.emit_trace(
            "tool_finished",
            tool=call.name,
            call_id=call.call_id,
            status=outcome.status,
            effect=outcome.side_effect_state,
        )
        return outcome

    def _finish_rejected(
        self,
        entry,
        call,
        code,
        detail,
        *,
        recovery="retry_after_change",
        structured=None,
    ):
        outcome = ToolOutcome(
            call.call_id,
            call.name,
            "rejected",
            "not_started",
            "none",
            "",
            structured=dict(structured or {}),
            failure=FailureInfo(code, detail, recovery),
        )
        return self._finish(entry, call, outcome)

    def _error_outcome(
        self, call, plan, code, detail, *, recovery="retry_after_change", unknown=False
    ):
        if call.name in {"run_shell", "verify"}:
            unknown = True
        if call.name in WRITE_TOOLS:
            receipt = next((r for r in reversed(self.runtime.session.mutations)
                            if r["id"] == getattr(self, "operation_id", None)), None)
            if receipt is not None:
                target = dict(plan.paths)[receipt["path"]]
                try:
                    actual = file_revision(target) if target.resolve() == target else "redirected"
                except OSError:
                    actual = "unavailable"
                if actual == receipt["before_revision"]:
                    receipt["status"] = "not_applied"
                    unknown = False
                else:
                    receipt["status"] = "unknown"
                    unknown = True
        effect_scope = "workspace" if unknown else "none"
        affected_paths = tuple(logical for logical, _target in plan.paths) if unknown else ()
        return ToolOutcome(
            call.call_id,
            call.name,
            "partial_success" if unknown else "error",
            "failed",
            "unknown" if unknown else "none",
            "",
            failure=FailureInfo(code, detail, recovery),
            affected_paths=affected_paths,
            effect_scope=effect_scope,
        )

    def _approve(self, call, args, tool, plan):
        name = call.name
        if not tool.get("risky"):
            return None
        handler = self.runtime.approval_handler
        if self.runtime.config.mode == "auto":
            return None
        if handler is not None and handler(name, deepcopy(args), deepcopy(plan)):
            return None
        return ToolOutcome(
            call.call_id,
            name,
            "rejected",
            "not_started",
            "none",
            "",
            failure=FailureInfo(
                "approval_denied", "operation was not approved", "user_action_required"
            ),
        )

    def _check_write_scope(self, plan):
        if not plan.paths:
            return
        allowed = self.effective_paths()
        if allowed is None:
            return
        denied = [logical for logical, _target in plan.paths if logical not in allowed]
        if denied:
            raise ToolFailureError(
                "write_scope_denied",
                "path is outside the task write scope: " + ", ".join(denied),
                "no_retry",
            )

    def _observed_revision(self, name, plan):
        if name != "edit_file":
            return
        logical, _target = plan.paths[0]
        observed = self.read_versions.get(logical)
        if observed is None:
            raise ToolFailureError(
                "read_required", "read the file before editing it", "retry_after_change"
            )
        return observed

    def _recheck_targets(self, name, args, plan):
        if name in WRITE_TOOLS:
            current = self.runtime.workspace.resolve_tool_path(args["path"])
            if current != plan.paths[0][1]:
                raise ToolFailureError(
                    "target_changed",
                    "the resolved file target changed during admission",
                    "retry_after_change",
                )
            return
        for logical, target in plan.paths:
            if self.runtime.workspace.resolve_tool_path(logical) != target:
                raise ToolFailureError(
                    "target_changed",
                    "an integration target changed during admission",
                    "retry_after_change",
                )

    def _observe(self, outcome):
        session = self.runtime.session
        path = outcome.structured.get("path")
        valid_read = (outcome.status == "success" or
                      (outcome.failure and outcome.failure.code == "missing_path"))
        if outcome.tool_name == "read_file" and path and valid_read and outcome.structured.get("revision"):
            revision = outcome.structured.get("revision")
            if revision:
                self.read_versions[path] = revision
            if path in session.file_states or any(r["path"] == path for r in session.mutations):
                if path in session.file_states and session.file_states[path] != revision:
                    outcome.structured["external_change_observed"] = True
                session.file_states[path] = revision
            retained = []
            for item in session.unconfirmed:
                if item["tool"] in WRITE_TOOLS and path in item["paths"]:
                    item["paths"] = [p for p in item["paths"] if p != path]
                    if not item["paths"]:
                        continue
                retained.append(item)
            session.unconfirmed = retained
        if (
            outcome.status == "success"
            and outcome.side_effect_state == "none"
            and outcome.tool_name in READ_TOOLS | {"run_shell"}
        ):
            for item in session.unconfirmed:
                if not item["paths"]:
                    item["observed"] = True
        if outcome.side_effect_state in {"changed", "partial"}:
            session.verification_required = True
            if outcome.tool_name == "run_shell":
                session.task_policy["verification_floor"] = True
            if session.verification["status"] == "passed":
                session.verification.update(status="stale", workspace_state=None)
        elif outcome.side_effect_state == "unknown":
            session.add_unconfirmed(
                f"tool:{len(session.history) - 1}:{outcome.tool_call_id}",
                outcome.tool_name,
                outcome.affected_paths,
            )


__all__ = ["ResolvedToolSurface", "ToolRuntime"]
