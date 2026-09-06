"""Explicit verification and integration of one immutable Child patch."""

from __future__ import annotations

import hashlib

from ..verification import verify_workspace
from ..workspace import clip
from .worktree import (
    GitWorktree,
    GitWorktreeError,
    apply_patch,
    require_clean_repository,
)


class PatchIntegrator:
    def __init__(self, manager):
        self.manager = manager
        self.parent = manager.parent

    def _verified_patch(self, run_id, record):
        projection = self.manager._child_projection(run_id, record)
        if projection.status != "completed":
            raise ValueError(f"Child Run did not complete: {record.child_id}")
        patch_receipt = record.completed().patch
        if patch_receipt is None:
            raise ValueError(f"Child patch is missing: {record.child_id}")
        path = self.manager._task_root(run_id, record.child_id) / "patch.diff"
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

    def _require_parent_base(self, base_sha, phase):
        try:
            current = require_clean_repository(
                self.parent.workspace.root,
                execution_context=self.parent.run.execution_context,
            )
        except GitWorktreeError as exc:
            raise ValueError(f"parent workspace changed {phase}: {exc}") from exc
        if current != base_sha:
            raise ValueError(f"parent base changed {phase}")

    def integrate_child(self, child_id):
        run_id = self.manager._parent_run_id()
        record = self.parent.run.projection.children.record(child_id)
        if record.spec.role != "implement":
            raise ValueError(f"Child is not an implementation: {child_id}")
        patch_receipt = record.completed().patch
        if patch_receipt is None:
            raise ValueError(f"Child has no patch: {child_id}")
        if patch_receipt.integrated:
            raise ValueError(f"Child patch is already integrated: {child_id}")
        patch = self._verified_patch(run_id, record)
        self._require_parent_base(record.base_sha, "after Child delegation")

        integration = GitWorktree(
            self.parent.workspace.root,
            record.base_sha,
            "integration-" + record.child_id,
            execution_context=self.parent.run.execution_context,
        )
        try:
            integration.create()
            integration.apply_patch(patch)
            changed_paths = integration.changed_paths()
            if changed_paths != patch_receipt.changed_paths:
                raise ValueError("integrated paths do not match the Child receipt")
            verification = self._verify_integration(integration)
            verified_paths = integration.changed_paths()
            verified_patch = integration.patch()
            if verified_paths != patch_receipt.changed_paths:
                raise ValueError("verification changed paths outside the Child receipt")
            if hashlib.sha256(verified_patch).hexdigest() != patch_receipt.sha256:
                raise ValueError("verification changed the immutable Child patch")
            self._require_parent_base(record.base_sha, "during patch verification")
            apply_patch(self.parent.workspace.root, patch,
                        execution_context=self.parent.run.execution_context)
        finally:
            integration.cleanup()

        handle = self.manager._release_worktree(run_id, record.child_id)
        if handle is not None:
            handle.cleanup()
        return {
            "status": "integrated",
            "child_id": record.child_id,
            "base_sha": record.base_sha,
            "changed_paths": list(patch_receipt.changed_paths),
            "verification": verification,
        }
