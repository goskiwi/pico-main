"""Parent-owned synchronous Pico child execution."""

from __future__ import annotations

import atexit
import uuid

from ..contracts import ToolOutcome
from ..run_store import RunStore
from ..session_store import SessionStore
from ..workspace import Workspace, normalize_relative_file
from .contracts import ChildFailure, ChildPatch, ChildRecord, ChildSpec, ChildSuccess
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
CHILD_MAX_AGENT_TURNS = 16


class SubagentRunner:
    def __init__(self, parent, model_client_factory):
        self.parent = parent
        self.model_client_factory = model_client_factory
        self._records_by_run: dict[str, dict[str, ChildRecord]] = {}
        self._recovered_runs: set[str] = set()
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
        records = self._records_by_run.setdefault(run_id, {})
        if run_id not in self._recovered_runs:
            self._recover_records(run_id, records)
            self._recovered_runs.add(run_id)
        return records

    def _recover_delegate(self, run_id, call, outcome, records):
        spec = ChildSpec.model_validate(call.args)
        if spec.role != "implement":
            return
        receipt = outcome.structured
        try:
            child_id = receipt["child_id"]
            role = receipt["role"]
            status = receipt["status"]
            child_run_id = receipt["child_run_id"]
            patch_receipt = receipt["patch"]
            base_sha = patch_receipt["base_sha"]
            raw_paths = patch_receipt["changed_paths"]
            patch_sha256 = patch_receipt["sha256"]
        except (KeyError, TypeError) as exc:
            raise ValueError("invalid persisted delegate receipt") from exc
        if not all(
            isinstance(value, str) and value
            for value in (child_id, child_run_id, base_sha, patch_sha256)
        ):
            raise ValueError("invalid persisted delegate receipt")
        if role != "implement" or status != "completed":
            raise ValueError("invalid persisted delegate receipt")
        if child_id in records or not isinstance(raw_paths, list):
            raise ValueError("invalid persisted delegate receipt")
        changed_paths = tuple(normalize_relative_file(path) for path in raw_paths)
        if not changed_paths or not set(changed_paths) <= set(
            spec.allowed_write_paths
        ):
            raise ValueError("persisted Child paths exceed the delegate call scope")
        subagent_root = self._run_root(run_id).resolve()
        patch_path = (subagent_root / child_id / "patch.diff").resolve()
        try:
            patch_path.relative_to(subagent_root)
        except ValueError as exc:
            raise ValueError("persisted Child patch path escapes its Run") from exc
        records[child_id] = ChildRecord(
            child_id=child_id,
            spec=spec,
            base_sha=base_sha,
            result=ChildSuccess(
                child_run_id,
                ChildPatch(
                    changed_paths,
                    str(patch_path),
                    patch_sha256,
                ),
            ),
        )

    def _recover_integration(self, call, outcome, records):
        try:
            child_id = call.args["child_id"]
            receipt_child_id = outcome.structured["child_id"]
        except (KeyError, TypeError) as exc:
            raise ValueError("invalid persisted integration receipt") from exc
        if (
            set(call.args) != {"child_id"}
            or not isinstance(child_id, str)
            or receipt_child_id != child_id
        ):
            raise ValueError("invalid persisted integration receipt")
        record = records.get(child_id)
        if record is None:
            raise ValueError("persisted integration references an unknown Child")
        record.mark_integrated()

    def _recover_records(self, run_id, records):
        calls = {}
        for event in self.parent.dependencies.run_store.read_events(run_id):
            if event.kind == "assistant_tool_call":
                calls[event.call_id] = event
                continue
            if event.kind != "tool_result":
                continue
            call = calls.get(event.call_id)
            if call is None or call.name not in {"delegate", "integrate_child"}:
                continue
            outcome = ToolOutcome.from_dict(event.payload["outcome"])
            if outcome.status != "success":
                continue
            if call.name == "delegate":
                self._recover_delegate(run_id, call, outcome, records)
            else:
                self._recover_integration(call, outcome, records)

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
        result = record.result
        child_run_id = result.child_run_id if result is not None else ""
        if not child_run_id:
            raise ValueError(f"child has no Run receipt: {record.child_id}")
        entries, projection = self._child_run_store(run_id, record).load_run(
            child_run_id
        )
        if not entries:
            raise ValueError(f"child Run Log is missing: {record.child_id}")
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
        workspace = Workspace.build(workspace_root)
        config = PicoConfig(
            mode=("ask" if record.spec.role == "explore" else "auto"),
            max_agent_turns=CHILD_MAX_AGENT_TURNS,
            max_tool_executions=CHILD_MAX_TOOL_EXECUTIONS,
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
            session_store=SessionStore(task_root / "sessions"),
            run_store=RunStore(task_root / "runs"),
            config=config,
            command_runner=(
                self.parent.dependencies.command_runner_factory(workspace_root)
            ),
            parent_execution_context=self.parent.run.execution_context,
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
                changed_paths = handle.changed_paths()
                unexpected = sorted(
                    set(changed_paths) - set(record.spec.allowed_write_paths)
                )
                if unexpected:
                    raise ValueError(
                        "write scope violation after execution: "
                        + ", ".join(unexpected)
                    )
                patch_path = self._task_root(run_id, record.child_id) / "patch.diff"
                patch = ChildPatch(
                    changed_paths,
                    str(patch_path.resolve()),
                    handle.write_patch(patch_path),
                )
            record.result = ChildSuccess(child_run_id, patch)
        except Exception as exc:  # noqa: BLE001 - preserve Child receipt on failure
            if child is not None and child.run.task is not None:
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
        self._records(run_id)[child_id] = record
        try:
            if spec.role == "implement":
                record.base_sha = require_clean_repository(
                    self.parent.workspace.root
                )
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
            self._discard_worktree(run_id, child_id)
        return self._receipt(run_id, record)

    def integrate_child(self, child_id):
        return self.integration.integrate_child(str(child_id))

    def completion_issue(self):
        records = self._records(self._parent_run_id())
        running = sorted(
            child_id
            for child_id, record in records.items()
            if record.result is None
        )
        if running:
            return "children are still running: " + ", ".join(running)
        unapplied = sorted(
            child_id
            for child_id, record in records.items()
            if record.spec.role == "implement"
            and isinstance(record.result, ChildSuccess)
            and record.result.patch is not None
            and not record.result.patch.integrated
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
