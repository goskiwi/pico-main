"""Parent-owned synchronous Pico child execution."""

from __future__ import annotations

import atexit
import uuid
from copy import deepcopy
from pathlib import Path

from ..contracts import FailureInfo, ToolExecutionPlan, ToolOutcome
from ..session_store import SessionStore
from ..workspace import Workspace
from .contracts import (
    ChildFailure,
    ChildLaunch,
    ChildPatch,
    ChildRecord,
    ChildSpec,
    ChildSuccess,
    planned_worktree_path,
)
from .integration import PatchIntegrator
from .worktree import GitClient, GitWorktree

EXPLORE_HANDOFF = """

Return a concise handoff for the parent Agent. Include:
- Findings: evidence-backed conclusions only.
- Evidence: exact repository paths, line ranges, and the smallest critical snippets.
- Unknowns: anything not established by tools.
- Recommended next step: the smallest useful follow-up.
The parent has not seen your tool transcript, so make the handoff sufficient without
asking it to repeat the whole investigation.
""".rstrip()

IMPLEMENT_HANDOFF = """

Runtime execution notes:
- Do not run git commands. This child uses an isolated Git Worktree whose immutable
  patch receipt is integrated only by an explicit parent action.
- Treat the parent task as the implementation specification; do not restart broad
  discovery.
- Read only the declared write paths and make the smallest complete change.
- Call submit_final after the change. The child Completion Gate runs the configured
  verification command against the final worktree.
""".rstrip()

EXPLORE_TOOLS = (
    "list_files",
    "read_file",
    "read_artifact",
    "search",
    "update_working_state",
)
IMPLEMENT_TOOLS = (
    "read_file",
    "read_artifact",
    "write_file",
    "edit_file",
    "update_working_state",
)
CHILD_MAX_TOOL_EXECUTIONS = 12
CHILD_MAX_AGENT_TURNS = 16


def _cleanup_recovered_worktree(runtime, record):
    if record.spec.role != "implement" or not record.worktree_path:
        return
    GitWorktree(
        runtime.workspace.root,
        record.base_sha,
        record.child_id,
        runtime.dependencies.command_runner_factory,
        runtime.run.execution_context,
        planned_path=Path(record.worktree_path),
    ).cleanup()


def recover_delegate(runtime, call):
    """Fail one interrupted synchronous Child without replay or adoption."""

    record = runtime.run.projection.children.record_for_call(call.call_id)
    if record is None:
        raise ValueError("interrupted delegate is missing its Child launch")
    try:
        detail = "synchronous Child execution was interrupted and was not resumed"
        return ToolOutcome(
            call.call_id,
            call.name,
            "error",
            "failed",
            "none",
            detail,
            structured={
                "child_id": record.child_id,
                "role": record.spec.role,
                "status": "failed",
                "error": detail,
            },
            failure=FailureInfo("child_interrupted", detail, "retry_after_change"),
        )
    finally:
        _cleanup_recovered_worktree(runtime, record)


class SubagentRunner:
    def __init__(self, parent, model_client_factory):
        self.parent = parent
        self.model_client_factory = model_client_factory
        self._worktrees: dict[tuple[str, str], GitWorktree] = {}
        self.integration = PatchIntegrator(parent)
        atexit.register(self.cleanup)

    def _parent_run_id(self):
        if self.parent.run.projection.contract is None:
            raise RuntimeError("subagent execution requires an active parent Run")
        return str(self.parent.run.projection.run_id)

    def _run_root(self, run_id):
        return self.parent.dependencies.run_store.run_dir(run_id) / "subagents"


    def _task_root(self, run_id, child_id):
        root = self._run_root(run_id) / child_id
        root.mkdir(parents=True, exist_ok=True)
        return root


    def _release_worktree(self, run_id, child_id):
        return self._worktrees.pop((run_id, child_id), None)

    def _discard_worktree(self, run_id, child_id):
        handle = self._release_worktree(run_id, child_id)
        if handle is not None:
            handle.cleanup()

    def _child_run_store(self, run_id, record):
        return SessionStore(self._task_root(run_id, record.child_id) / "sessions").runs(record.child_id)

    def _child_projection(self, run_id, record):
        result = record.result
        child_run_id = result.child_run_id if result is not None else ""
        if not child_run_id:
            raise ValueError(f"child has no Run receipt: {record.child_id}")
        return self._child_run_store(run_id, record).load_run(
            child_run_id
        ).projection

    def _receipt(self, run_id, record):
        result = record.result
        receipt = {
            "child_id": record.child_id,
            "role": record.spec.role,
            "status": record.status,
        }
        if result is None:
            return receipt
        if result.child_run_id:
            receipt["child_run_id"] = result.child_run_id
        if isinstance(result, ChildFailure):
            receipt["error"] = result.error
            return receipt
        record.completed()
        projection = self._child_projection(run_id, record)
        receipt["result"] = self.parent.redact_text(projection.final_answer)
        if result.patch is not None:
            receipt["patch"] = {
                "base_sha": record.base_sha,
                "changed_paths": list(result.patch.changed_paths),
                "sha256": result.patch.sha256,
                "integrated": result.patch.integrated,
            }
        return receipt

    def _prepare_implement_worktree(self, run_id, record):
        handle = GitWorktree(
            self.parent.workspace.root,
            record.base_sha,
            record.child_id,
            self.parent.dependencies.command_runner_factory,
            self.parent.run.execution_context,
            planned_path=Path(record.worktree_path),
        )
        handle.create()
        self._worktrees[(run_id, record.child_id)] = handle
        self.integration.prepare_child_input(handle, record)
        return handle

    def _build_child(self, run_id, record):
        from ..runtime import Pico, PicoConfig

        task_root = self._task_root(run_id, record.child_id)
        workspace_root = (
            self._worktrees[(run_id, record.child_id)].path
            if record.spec.role == "implement"
            else self.parent.workspace.root
        )
        workspace = Workspace.build(workspace_root)
        config = PicoConfig(
            mode=("ask" if record.spec.role == "explore" else "auto"),
            max_agent_turns=CHILD_MAX_AGENT_TURNS,
            max_tool_executions=CHILD_MAX_TOOL_EXECUTIONS,
            max_parallel_tools=self.parent.config.max_parallel_tools,
            max_new_tokens=self.parent.config.max_new_tokens,
            summary_max_output_tokens=self.parent.config.summary_max_output_tokens,
            secret_env_names=self.parent.config.secret_env_names,
            allowed_tools=(
                EXPLORE_TOOLS
                if record.spec.role == "explore"
                else IMPLEMENT_TOOLS
            ),
            allowed_write_paths=record.spec.allowed_write_paths,
            turn_timeout_seconds=self.parent.config.turn_timeout_seconds,
            provider_context_limit_tokens=(
                self.parent.config.provider_context_limit_tokens
            ),
            compaction_reserve_tokens=(
                self.parent.config.compaction_reserve_tokens
            ),
            compaction_keep_recent_tokens=(
                self.parent.config.compaction_keep_recent_tokens
            ),
            verification_command=(
                ""
                if record.spec.role == "explore"
                else record.verification_command
            ),
        )
        sessions = SessionStore(task_root / "sessions")
        return Pico.create(
            model_client=self.model_client_factory(record.spec),
            workspace=workspace,
            config=config,
            command_runner=self.parent.dependencies.command_runner_factory(
                workspace_root
            ),
            parent_execution_context=self.parent.run.execution_context,
            session_store=sessions,
            session_id=record.child_id,
        )

    def _run_child(self, run_id, record):
        child = None
        child_run_id = ""
        prompt = record.spec.task
        if record.spec.role == "explore":
            prompt += EXPLORE_HANDOFF
        else:
            prompt += (
                IMPLEMENT_HANDOFF
                + "\nConfigured verification command:\n"
                + record.verification_command
            )
        try:
            child = self._build_child(run_id, record)
            child_outcome = child.ask(prompt)
            child_run_id = child_outcome.run_id
            projection = child.dependencies.run_store.replay(child_run_id)
            if projection.status != "completed":
                raise RuntimeError(projection.stop_reason or projection.status)
            patch = None
            if record.spec.role == "implement":
                handle = self._worktrees[(run_id, record.child_id)]
                projection.evidence.change_set.require_current_workspace(handle.path)
                changed_paths = tuple(projection.evidence.changed_paths)
                git_paths = set(handle.changed_paths())
                unexpected = sorted(
                    (set(changed_paths) | git_paths) - set(record.spec.allowed_write_paths)
                )
                if unexpected:
                    raise ValueError(
                        "write scope violation after execution: "
                        + ", ".join(unexpected)
                    )
                if git_paths - set(changed_paths):
                    raise ValueError("Child workspace changes are missing from the Run")
                if changed_paths:
                    patch_path = self._task_root(run_id, record.child_id) / "patch.diff"
                    patch = ChildPatch(changed_paths, handle.write_patch(patch_path, changed_paths))
            return ChildSuccess(child_run_id, patch)
        except Exception as exc:  # noqa: BLE001 - preserve Child receipt on failure
            if child is not None and child.run.projection.contract is not None:
                child_run_id = child.run.projection.run_id
            return ChildFailure(
                self.parent.redact_text(f"{type(exc).__name__}: {exc}"),
                child_run_id,
            )

    def plan_delegate(self, call_id, role, task, allowed_write_paths=()):
        spec = ChildSpec(role=role, task=task, allowed_write_paths=allowed_write_paths)
        run_id = self._parent_run_id()
        records = self.parent.run.projection.children.records
        while True:
            child_id = "child_" + uuid.uuid4().hex[:12]
            if child_id not in records:
                break
        base_sha = ""
        verification_command = ""
        worktree_path = ""
        if spec.role == "implement" and not str(
            self.parent.config.verification_command or ""
        ).strip():
            raise ValueError("implement children require a verification command")
        if spec.role == "implement":
            verification_command = str(
                self.parent.config.verification_command
            ).strip()
            base_sha = GitClient(
                self.parent.workspace.root,
                self.parent.dependencies.command_runner_factory,
                self.parent.run.execution_context,
            ).run("rev-parse", "HEAD").decode().strip()
            worktree_path = planned_worktree_path(run_id, child_id)
            record = ChildRecord(
                child_id,
                call_id,
                spec,
                base_sha,
                verification_command,
                worktree_path,
            )
            self.integration._parent_changes(record)
        launch = ChildLaunch(
            child_id,
            base_sha,
            verification_command,
            worktree_path,
        ).validate_for(spec, run_id)
        return ToolExecutionPlan("none", operation=launch.to_dict())

    def delegate(self, plan, role, task, allowed_write_paths=()):
        if not isinstance(plan, ToolExecutionPlan):
            raise TypeError("delegate requires its persisted ToolExecutionPlan")
        spec = ChildSpec(role=role, task=task, allowed_write_paths=allowed_write_paths)
        run_id = self._parent_run_id()
        launch = ChildLaunch.from_dict(plan.operation).validate_for(spec, run_id)
        projected = self.parent.run.projection.children.record(launch.child_id)
        if projected.parent_call_id == "" or projected.spec != spec:
            raise ValueError("persisted Child launch does not match delegate")
        record = deepcopy(projected)
        try:
            if spec.role == "implement":
                self.integration._parent_changes(record)
                self._prepare_implement_worktree(run_id, record)
            record.result = self._run_child(run_id, record)
        except Exception as exc:  # noqa: BLE001 - Child failure is receipt state
            child_run_id = (
                record.result.child_run_id
                if record.result is not None
                else ""
            )
            record.result = ChildFailure(
                self.parent.redact_text(f"{type(exc).__name__}: {exc}"),
                child_run_id,
            )
        try:
            receipt = self._receipt(run_id, record)
        except Exception as exc:  # noqa: BLE001 - launched Child needs one receipt
            child_run_id = (
                record.result.child_run_id
                if record.result is not None
                else ""
            )
            record.result = ChildFailure(
                self.parent.redact_text(f"{type(exc).__name__}: {exc}"),
                child_run_id,
            )
            receipt = self._receipt(run_id, record)
        self._discard_worktree(run_id, launch.child_id)
        return receipt

    def integrate_child(self, child_id, plan):
        result = self.integration.integrate_child(str(child_id), plan)
        self._discard_worktree(self._parent_run_id(), str(child_id))
        return result


    def cleanup(self):
        for handle in list(self._worktrees.values()):
            handle.cleanup()
        self._worktrees.clear()
