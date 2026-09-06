"""Build, verify and publish Child changes against the current accepted Parent state."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from dataclasses import replace

from ..contracts import ToolCall, ToolOutcome
from ..mutations import ABSENT_REVISION, content_revision, file_revision
from ..persistence import atomic_replace_bytes
from ..run_log import replay_events
from ..run_store import RunStore
from ..verification import verify_workspace
from ..workspace import clip
from .worktree import GitWorktree, GitWorktreeError, _git, repository_changed_paths


class PatchIntegrator:
    def __init__(self, parent):
        self.parent = parent

    def _verified_patch(self, run_id, record):
        task_root = (
            self.parent.dependencies.run_store.run_dir(run_id)
            / "subagents" / record.child_id
        )
        _log, projection = RunStore(task_root / "runs").load_run(
            record.completed().child_run_id
        )
        if projection.status != "completed":
            raise ValueError(f"Child Run did not complete: {record.child_id}")
        patch_receipt = record.completed().patch
        if patch_receipt is None:
            raise ValueError(f"Child patch is missing: {record.child_id}")
        path = task_root / "patch.diff"
        if not path.is_file():
            raise ValueError(f"Child patch is missing: {record.child_id}")
        patch = path.read_bytes()
        digest = hashlib.sha256(patch).hexdigest()
        if digest != patch_receipt.sha256:
            raise ValueError(f"Child patch digest is invalid: {record.child_id}")
        return patch

    def _verify_integration(self, worktree):
        command = str(self.parent.config.verification_command or "").strip()
        if not command:
            raise ValueError("Child integration requires a verification command")
        parent_execution = self.parent.run.execution_context
        if parent_execution is None:
            raise RuntimeError("Child integration requires an active Parent turn")
        execution = parent_execution.child()
        verification = verify_workspace(
            root=worktree.path,
            command=command,
            command_runner=(
                self.parent.dependencies.command_runner_factory(worktree.path)
            ),
            timeout_seconds=self.parent.config.turn_timeout_seconds,
            redact_text=self.parent.redact_text,
            mutation_sequence_provider=lambda: 0,
            started_workspace_mutation_sequence=0,
            changed_paths=worktree.changed_paths(),
            execution_context=execution,
        )
        if not verification or verification["status"] != "passed":
            detail = str((verification or {}).get("output", ""))
            raise RuntimeError(
                "integrated Child verification failed"
                + (f": {clip(detail, 2000)}" if detail else "")
            )
        return verification

    @staticmethod
    def _file_data(path):
        if path.is_symlink():
            raise ValueError(f"integration path must not be a symlink: {path}")
        if not path.exists():
            return None, 0o644
        if not path.is_file():
            raise ValueError(f"integration path is not a file: {path}")
        return path.read_bytes(), path.stat().st_mode & 0o777

    @staticmethod
    def _revision(data):
        return ABSENT_REVISION if data is None else content_revision(data)

    def _parent_changes(self, record, observed=None):
        root = self.parent.workspace.root
        execution = self.parent.run.execution_context
        head = _git(root, "rev-parse", "HEAD", execution_context=execution).decode().strip()
        if head != record.base_sha:
            raise ValueError("parent base changed after Child delegation")
        if _git(root, "diff", "--cached", "--name-only", "-z", execution_context=execution):
            raise ValueError("parent index changed after Child delegation")
        evidence = self.parent.run.evidence
        observed = observed or {}
        allowed = set(evidence.changed_paths) | set(observed)
        extra = set(repository_changed_paths(root, execution_context=execution)) - allowed
        if extra:
            raise ValueError("parent has unrecorded workspace changes: " + ", ".join(sorted(extra)))
        changes = {}
        for path in sorted(set(evidence.touched_paths) | set(observed)):
            current = self._file_data(self.parent.workspace.resolve_tool_path(path))
            expected = observed[path] if path in observed else evidence.change_set.files[path].current_after_state
            if self._revision(current[0]) != expected:
                raise ValueError("parent workspace changed outside this Run: " + path)
            if path in allowed:
                changes[path] = current
        return changes

    @staticmethod
    def _copy_changes(tree, changes):
        for path, (data, mode) in changes.items():
            target = tree.path / path
            if data is None:
                target.unlink(missing_ok=True)
            else:
                atomic_replace_bytes(target, data, mode=mode)
        if changes:
            tracked = set(_git(tree.path, "ls-files", "-z", "--", *changes,
                               execution_context=tree.execution_context).decode().split("\0"))
            paths = [p for p in changes if (tree.path / p).exists() or p in tracked]
            if paths:
                _git(tree.path, "add", "-A", "--force", "--", *paths,
                     execution_context=tree.execution_context)

    def prepare_child_input(self, tree, record):
        changes = self._parent_changes(record)
        if not changes:
            return
        self._copy_changes(tree, changes)
        # Give the isolated Child a Git input commit so its patch contains only
        # its own changes. The Parent HEAD/index are never committed or staged.
        _git(tree.path, "-c", "user.name=Pico", "-c", "user.email=pico@example.invalid",
             "commit", "--quiet", "--allow-empty", "-m", "Pico Child input",
             execution_context=tree.execution_context)
        if self._parent_changes(record) != changes:
            raise ValueError("parent changed while preparing Child input")

    @contextmanager
    def _candidate(self, record, patch, changes, before):
        tree = GitWorktree(self.parent.workspace.root, record.base_sha,
                           "integration-" + record.child_id,
                           execution_context=self.parent.run.execution_context)
        try:
            tree.create()
            for path, state in before.items():
                if path not in changes and file_revision(tree.path / path) != state:
                    raise ValueError("parent path changed after Child delegation: " + path)
            for path, (_data, mode) in changes.items():
                base = tree.path / path
                expected_mode = base.stat().st_mode & 0o777 if base.is_file() else 0o644
                if mode != expected_mode:
                    raise ValueError("parent file mode changed outside this Run: " + path)
            self._copy_changes(tree, changes)
            before_tree = _git(tree.path, "write-tree", execution_context=tree.execution_context).decode().strip()
            _git(tree.path, "apply", "--3way", "--whitespace=nowarn", "-",
                 input_bytes=patch, execution_context=tree.execution_context)
            delta, paths = self._delta(tree, before_tree)
            if set(paths) - set(record.completed().patch.changed_paths):
                raise ValueError("integration changed paths outside the Child receipt")
            yield tree, before_tree, delta, paths
        finally:
            tree.cleanup()

    @staticmethod
    def _delta(tree, before_tree):
        common = ("--no-renames", "--no-ext-diff", "--no-textconv")
        delta = _git(tree.path, "diff", "--binary", *common, before_tree,
                     execution_context=tree.execution_context)
        paths = _git(tree.path, "diff", "--name-only", "-z", *common, before_tree,
                     execution_context=tree.execution_context).decode().split("\0")
        return delta, tuple(sorted(p for p in paths if p))

    def recover_applied(self, call, started, outcome):
        record = self.parent.run.projection.children.records.get(call.args.get("child_id"))
        if record is None or outcome.side_effect_state != "partial":
            return outcome
        receipt = record.completed().patch
        if receipt is None:
            return outcome
        patch = self._verified_patch(self.parent.run.projection.run_id, record)
        effects = {item["path"]: item for item in started.payload["potential_effects"]}
        if set(effects) != set(receipt.changed_paths):
            return outcome
        after = {item["path"]: item["after_state"]
                 for item in outcome.structured.get("path_transitions", ())}
        pending = self.parent.run.run_log.pending_tool_starts().get(call.call_id) == started
        try:
            self._parent_changes(record, after if pending else None)
            prior = replay_events(e for e in self.parent.run.run_log.events if e.sequence < started.sequence)
            changes = {}
            before = {p: item["before_state"] for p, item in effects.items()}
            for path, item in effects.items():
                if item["before_state"] == ABSENT_REVISION:
                    continue
                _descriptor, data = self.parent.dependencies.artifacts.read_internal(
                    self.parent.run.projection.run_id, item["before_artifact_id"],
                    expected_kind="workspace_preimage",
                )
                if self._revision(data) != item["before_state"]:
                    return outcome
                mode = self._file_data(self.parent.workspace.resolve_tool_path(path))[1]
                if path in prior.evidence.touched_paths:
                    changes[path] = (data, mode)
            # Base files supply untouched before-images. Previously modified paths
            # use this transaction's persisted preimage, not today's file contents.
            with self._candidate(record, patch, changes, before) as (tree, _base, _delta, _paths):
                for path in receipt.changed_paths:
                    expected = file_revision(tree.path / path)
                    if expected != after.get(path, before[path]):
                        return outcome
                    actual_mode = self._file_data(self.parent.workspace.resolve_tool_path(path))[1]
                    if actual_mode != self._file_data(tree.path / path)[1]:
                        return outcome
        except (OSError, ValueError, GitWorktreeError):
            return outcome
        return replace(outcome, structured={
            **outcome.structured, "status": "integrated", "child_id": record.child_id,
            "base_sha": record.base_sha, "changed_paths": list(receipt.changed_paths),
        }, content=outcome.content + "; Child application confirmed; current-state verification is required")

    def _applied_receipt_from_history(self, child_id):
        call = started = None
        for event in self.parent.run.run_log.events:
            if event.kind in {"assistant_tool_call", "assistant_tool_batch"}:
                call = (ToolCall(event.name, event.args, event.call_id)
                        if event.name == "integrate_child" and event.args.get("child_id") == child_id else None)
                started = None
            elif call is not None and event.kind == "tool_started" and event.call_id == call.call_id:
                started = event
            elif call is not None and started is not None and event.kind == "tool_result":
                outcome = ToolOutcome.from_dict(event.payload["outcome"])
                confirmed = self.recover_applied(call, started, outcome)
                if confirmed.structured.get("status") == "integrated":
                    return confirmed.structured
        return None

    def _publish(self, tree, paths, before):
        mutations = self.parent.dependencies.mutations
        with mutations._lock:
            for path in paths:
                target = self.parent.workspace.resolve_tool_path(path)
                mutations._require_revision(target, path, before[path])
            for path in paths:
                target = self.parent.workspace.resolve_tool_path(path)
                data, _mode = self._file_data(tree.path / path)
                if data is None:
                    mutations._require_revision(target, path, before[path])
                    target.unlink()
                else:
                    mutations._commit(target, path, data, before[path])

    def integrate_child(self, child_id):
        record = self.parent.run.projection.children.record(child_id)
        receipt = record.completed().patch
        if record.spec.role != "implement" or receipt is None:
            raise ValueError(f"Child has no implementation patch: {child_id}")
        if receipt.integrated:
            raise ValueError(f"Child patch is already integrated: {child_id}")
        confirmed = self._applied_receipt_from_history(child_id)
        if confirmed is not None:
            return confirmed
        patch = self._verified_patch(self.parent.run.projection.run_id, record)
        changes = self._parent_changes(record)
        before = {path: file_revision(self.parent.workspace.resolve_tool_path(path)) for path in receipt.changed_paths}
        for call in self.parent.run.run_log.pending_tool_calls():
            if call.name == "integrate_child" and call.args.get("child_id") == child_id:
                started = self.parent.run.run_log.pending_tool_starts().get(call.call_id)
                if started is not None and before != {
                    item["path"]: item["before_state"] for item in started.payload["potential_effects"]
                }:
                    raise ValueError("parent changed after integration started")
        with self._candidate(record, patch, changes, before) as (tree, base, delta, paths):
            verification = self._verify_integration(tree)
            if self._delta(tree, base) != (delta, paths):
                raise ValueError("verification changed the combined Child result")
            if self._parent_changes(record) != changes:
                raise ValueError("parent changed during integration verification")
            self._publish(tree, paths, before)
        return {"status": "integrated", "child_id": child_id, "base_sha": record.base_sha,
                "changed_paths": list(receipt.changed_paths), "verification": verification}
