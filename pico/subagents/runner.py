"""Parent-owned synchronous Pico child execution."""

from __future__ import annotations

import atexit
import uuid
from dataclasses import replace

from ..run_store import RunStore
from ..session_store import SessionStore
from ..workspace import Workspace
from .contracts import ChildFailure, ChildPatch, ChildRecord, ChildSpec, ChildSuccess
from .integration import PatchIntegrator
from .worktree import GitWorktree, _git

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
    "write_file",
    "edit_file",
    "update_working_state",
)
CHILD_MAX_TOOL_EXECUTIONS = 12
CHILD_MAX_AGENT_TURNS = 16


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


    def _new_child_id(self, run_id):
        records = self.parent.run.projection.children.records
        while True:
            child_id = "child_" + uuid.uuid4().hex[:12]
            if child_id not in records:
                return child_id

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
        return RunStore(self._task_root(run_id, record.child_id) / "runs")

    def _child_projection(self, run_id, record):
        result = record.result
        child_run_id = result.child_run_id if result is not None else ""
        if not child_run_id:
            raise ValueError(f"child has no Run receipt: {record.child_id}")
        _log, projection = self._child_run_store(run_id, record).load_run(
            child_run_id
        )
        return projection

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
            execution_context=self.parent.run.execution_context,
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
                else self.parent.config.verification_command
            ),
        )
        return Pico(
            model_client=self.model_client_factory(record.spec),
            workspace=workspace,
            run_store=RunStore(task_root / "runs"),
            config=config,
            command_runner=self.parent.dependencies.command_runner_factory(
                workspace_root
            ),
            parent_execution_context=self.parent.run.execution_context,
            session=SessionStore(task_root / "sessions").create(workspace.root),
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
                + str(self.parent.config.verification_command).strip()
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
            record.result = ChildSuccess(child_run_id, patch)
        except Exception as exc:  # noqa: BLE001 - preserve Child receipt on failure
            if child is not None and child.run.projection.contract is not None:
                child_run_id = child.run.projection.run_id
            record.result = ChildFailure(
                self.parent.redact_text(f"{type(exc).__name__}: {exc}"),
                child_run_id,
            )

    def delegate(self, role, task, allowed_write_paths=()):
        spec = ChildSpec(
            role=role,
            task=task,
            allowed_write_paths=allowed_write_paths,
        )
        if spec.role == "implement" and not str(
            self.parent.config.verification_command or ""
        ).strip():
            raise ValueError("implement children require a verification command")
        run_id = self._parent_run_id()
        child_id = self._new_child_id(run_id)
        record = ChildRecord(child_id=child_id, spec=spec)
        try:
            if spec.role == "implement":
                record.base_sha = _git(
                    self.parent.workspace.root, "rev-parse", "HEAD",
                    execution_context=self.parent.run.execution_context,
                ).decode().strip()
                self.integration._parent_changes(record)
                self._prepare_implement_worktree(run_id, record)
            self._run_child(run_id, record)
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
        if isinstance(record.result, ChildFailure):
            handle = self._release_worktree(run_id, child_id)
            if handle is not None:
                record.result = replace(
                    record.result,
                    error=record.result.error + f"; retained worktree: {handle.path}",
                )
        elif record.result.patch is None:
            self._discard_worktree(run_id, child_id)
        return self._receipt(run_id, record)

    def integrate_child(self, child_id):
        result = self.integration.integrate_child(str(child_id))
        self._discard_worktree(self._parent_run_id(), str(child_id))
        return result


    def cleanup(self):
        for handle in list(self._worktrees.values()):
            handle.cleanup()
        self._worktrees.clear()
