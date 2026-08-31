"""Parent-owned synchronous Pico child execution."""

from __future__ import annotations

import atexit
import uuid

from ..run_store import RunStore
from ..session_store import SessionStore
from ..workspace import WorkspaceContext
from .contracts import ChildRecord, ChildSpec
from .integration import PatchIntegrator
from .worktree import GitWorktree, require_clean_repository

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


class SubagentRunner:
    def __init__(self, parent, model_client_factory):
        self.parent = parent
        self.model_client_factory = model_client_factory
        self._records_by_run: dict[str, dict[str, ChildRecord]] = {}
        self._worktrees: dict[tuple[str, str], GitWorktree] = {}
        self.integration = PatchIntegrator(self)
        atexit.register(self.cleanup)

    def _parent_run_id(self):
        if self.parent.run.task is None:
            raise RuntimeError("subagent execution requires an active parent Run")
        return str(self.parent.run.projection.run_id)

    def _run_root(self, run_id):
        return self.parent.dependencies.run_store.run_dir(run_id) / "subagents"

    def _records(self, run_id):
        return self._records_by_run.setdefault(run_id, {})

    def _new_child_id(self, run_id):
        records = self._records(run_id)
        while True:
            child_id = "child_" + uuid.uuid4().hex[:12]
            if child_id not in records:
                return child_id

    def _task_root(self, run_id, child_id):
        root = self._run_root(run_id) / child_id
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _record(self, run_id, child_id):
        record = self._records(run_id).get(str(child_id))
        if record is None:
            raise ValueError(f"unknown child: {child_id}")
        return record

    def _release_worktree(self, run_id, child_id):
        return self._worktrees.pop((run_id, child_id), None)

    def _discard_worktree(self, run_id, child_id):
        handle = self._release_worktree(run_id, child_id)
        if handle is not None:
            handle.cleanup()

    def _child_run_store(self, run_id, record):
        return RunStore(self._task_root(run_id, record.child_id) / "runs")

    def _child_projection(self, run_id, record):
        if not record.child_run_id:
            raise ValueError(f"child has no Run receipt: {record.child_id}")
        entries, projection = self._child_run_store(run_id, record).load_run(
            record.child_run_id
        )
        if not entries:
            raise ValueError(f"child Run Log is missing: {record.child_id}")
        return projection

    def _receipt(self, run_id, record):
        projection = (
            self._child_projection(run_id, record)
            if record.child_run_id
            else None
        )
        error = record.error
        if not error and projection is not None and projection.status != "completed":
            error = projection.stop_reason or projection.status
        return {
            "child_id": record.child_id,
            "role": record.spec.role,
            "status": record.status,
            "child_run_id": record.child_run_id,
            "result": self.parent.redact_text(
                projection.final_answer if projection is not None else ""
            ),
            "base_sha": record.base_sha,
            "changed_paths": list(record.changed_paths),
            "patch_sha256": record.patch_sha256,
            "error": error,
            "integrated": record.integrated,
        }

    def _prepare_implement_worktree(self, run_id, record):
        handle = GitWorktree(
            self.parent.workspace.root,
            record.base_sha,
            record.child_id,
        )
        handle.create()
        self._worktrees[(run_id, record.child_id)] = handle
        return handle

    def _build_child(self, run_id, record):
        from ..runtime import Pico, PicoConfig

        task_root = self._task_root(run_id, record.child_id)
        workspace_root = (
            self._worktrees[(run_id, record.child_id)].path
            if record.spec.role == "implement"
            else self.parent.workspace.root
        )
        workspace = WorkspaceContext.build(workspace_root)
        config = PicoConfig(
            approval_policy="auto",
            max_tool_executions=CHILD_MAX_TOOL_EXECUTIONS,
            max_new_tokens=self.parent.config.max_new_tokens,
            secret_env_names=self.parent.config.secret_env_names,
            allowed_tools=(
                EXPLORE_TOOLS
                if record.spec.role == "explore"
                else IMPLEMENT_TOOLS
            ),
            allowed_write_paths=record.spec.allowed_write_paths,
            run_timeout_seconds=self.parent.config.run_timeout_seconds,
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
            session_store=SessionStore(task_root / "sessions"),
            run_store=RunStore(task_root / "runs"),
            config=config,
            command_runner=(
                self.parent.dependencies.command_runner_factory(workspace_root)
            ),
            parent_cancellation_token=(
                self.parent.run.execution_context.token
                if self.parent.run.execution_context is not None
                else None
            ),
        )

    def _run_child(self, run_id, record):
        child = self._build_child(run_id, record)
        prompt = record.spec.task
        if record.spec.role == "explore":
            prompt += EXPLORE_HANDOFF
        else:
            prompt += (
                IMPLEMENT_HANDOFF
                + "\nConfigured verification command:\n"
                + str(self.parent.config.verification_command).strip()
            )
        child_error = None
        child_outcome = None
        try:
            child_outcome = child.ask(
                prompt,
                task_kind=(
                    "read_only" if record.spec.role == "explore" else "modify"
                ),
                requires_workspace_change=record.spec.role == "implement",
                requires_verification=record.spec.role == "implement",
            )
        except Exception as exc:  # noqa: BLE001 - preserve Child receipt on failure
            child_error = exc

        projection = None
        if child_outcome is not None:
            record.child_run_id = child_outcome.run_id
            projection = child.dependencies.run_store.replay(record.child_run_id)
        elif child.run.task is not None:
            record.child_run_id = child.run.projection.run_id
            projection = child.dependencies.run_store.replay(record.child_run_id)

        if record.spec.role == "implement":
            handle = self._worktrees[(run_id, record.child_id)]
            record.changed_paths = handle.changed_paths()
            unexpected = sorted(
                set(record.changed_paths) - set(record.spec.allowed_write_paths)
            )
            if unexpected:
                raise ValueError(
                    "write scope violation after execution: "
                    + ", ".join(unexpected)
                )
        if child_error is not None:
            raise child_error
        if projection is None:
            raise RuntimeError("child Run did not produce a replayable result")
        if projection.status != "completed":
            raise RuntimeError(projection.stop_reason or projection.status)

        if record.spec.role == "implement":
            handle = self._worktrees[(run_id, record.child_id)]
            patch_path = self._task_root(run_id, record.child_id) / "patch.diff"
            record.patch_sha256 = handle.write_patch(patch_path)
            record.patch_path = str(patch_path.resolve())
        record.status = "completed"
        record.error = ""

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
        self._records(run_id)[child_id] = record
        try:
            if spec.role == "implement":
                record.base_sha = require_clean_repository(
                    self.parent.workspace.root
                )
                self._prepare_implement_worktree(run_id, record)
            self._run_child(run_id, record)
        except Exception as exc:  # noqa: BLE001 - Child failure is receipt state
            record.status = "failed"
            record.error = self.parent.redact_text(
                f"{type(exc).__name__}: {exc}"
            )
            self._discard_worktree(run_id, child_id)
        return self._receipt(run_id, record)

    def integrate_child(self, child_id):
        return self.integration.integrate_child(str(child_id))

    def completion_issue(self):
        records = self._records(self._parent_run_id())
        running = sorted(
            child_id
            for child_id, record in records.items()
            if record.status == "running"
        )
        if running:
            return "children are still running: " + ", ".join(running)
        unapplied = sorted(
            child_id
            for child_id, record in records.items()
            if record.spec.role == "implement"
            and record.status == "completed"
            and not record.integrated
        )
        if unapplied:
            return "completed implementation patches are not integrated: " + ", ".join(
                unapplied
            )
        return ""

    def cleanup(self):
        for handle in list(self._worktrees.values()):
            handle.cleanup()
        self._worktrees.clear()
