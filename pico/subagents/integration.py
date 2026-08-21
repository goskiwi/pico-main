"""Receipt-bound verification and integration of Child patches."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

from ..execution import ExecutionContext
from ..verification import verify_workspace
from ..workspace import clip
from .dag import implementation_order
from .worktree import (
    GitWorktree,
    apply_patch,
    require_clean_repository,
)


class PatchIntegrator:
    def __init__(
        self,
        *,
        parent,
        parent_run_id: Callable[[], str],
        records: Callable[[str], dict],
        outcome_status: Callable[[str, object], str],
        child_projection: Callable[[str, object], object],
        release_worktree: Callable[[str, str], object],
    ):
        self.parent = parent
        self.parent_run_id = parent_run_id
        self.records = records
        self.outcome_status = outcome_status
        self.child_projection = child_projection
        self.release_worktree = release_worktree

    def verify_record_receipt(self, run_id, record):
        projection = self.child_projection(run_id, record)
        if projection.status != "completed":
            raise ValueError(
                f"subtask child run did not complete: {record.spec.task_id}"
            )
        if record.spec.kind == "implement":
            path = Path(record.patch_path)
            if not path.is_file():
                raise ValueError(f"subtask patch is missing: {record.spec.task_id}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != record.patch_sha256:
                raise ValueError(
                    f"subtask patch digest is invalid: {record.spec.task_id}"
                )
        return projection

    def _verify_integration(self, worktree):
        command = str(self.parent.config.verification_command or "").strip()
        if not command:
            raise ValueError("patch integration requires a verification command")
        execution = ExecutionContext.standalone(
            owner="subtask_integration_verifier",
            max_seconds=min(120, self.parent.config.run_timeout_seconds),
        )
        workspace_fingerprint = hashlib.sha256(worktree.patch()).hexdigest()
        verification = verify_workspace(
            root=worktree.path,
            command=command,
            sandbox=self.parent.services.sandbox_factory(worktree.path),
            timeout_seconds=self.parent.config.run_timeout_seconds,
            redact_text=self.parent.redact_text,
            fingerprint_provider=lambda: hashlib.sha256(worktree.patch()).hexdigest(),
            workspace_fingerprint=workspace_fingerprint,
            execution_context=execution,
        )
        if not verification or verification["status"] != "passed":
            detail = str((verification or {}).get("output", ""))
            raise RuntimeError(
                "integrated subtask verification failed"
                + (f": {clip(detail, 2000)}" if detail else "")
            )
        return verification

    def apply(self, task_ids):
        run_id = self.parent_run_id()
        records = self.records(run_id)
        for task_id in task_ids:
            if task_id not in records:
                raise ValueError(f"unknown subtask: {task_id}")
            if records[task_id].spec.kind != "implement":
                raise ValueError(f"subtask is not an implementation: {task_id}")
        order = implementation_order(records, tuple(task_ids))
        selected = [records[task_id] for task_id in order]
        if any(
            self.outcome_status(run_id, record) != "completed"
            for record in selected
        ):
            raise ValueError("all implementation subtasks must be completed")
        if any(record.applied for record in selected):
            raise ValueError("implementation subtask was already integrated")
        for record in selected:
            self.verify_record_receipt(run_id, record)
        base_shas = {record.base_sha for record in selected}
        if len(base_shas) != 1:
            raise ValueError("implementation patches do not share one base revision")
        base_sha = next(iter(base_shas))
        if require_clean_repository(self.parent.workspace.root) != base_sha:
            raise ValueError("parent workspace changed after implementation delegation")

        integration = GitWorktree(self.parent.workspace.root, base_sha, "integration")
        try:
            integration.create()
            for record in selected:
                if not record.patch_path:
                    raise ValueError(
                        f"implementation subtask has no patch: {record.spec.task_id}"
                    )
                integration.apply_patch(Path(record.patch_path).read_bytes())
            verification = self._verify_integration(integration)
            aggregate = integration.patch()
            if not aggregate:
                raise ValueError("integrated subtasks produced no aggregate patch")
            if require_clean_repository(self.parent.workspace.root) != base_sha:
                raise ValueError("parent workspace changed during patch verification")
            apply_patch(self.parent.workspace.root, aggregate)
        finally:
            integration.cleanup()

        for record in selected:
            record.applied = True
            handle = self.release_worktree(run_id, record.spec.task_id)
            if handle is not None:
                handle.cleanup()
        return {
            "status": "applied",
            "task_ids": list(order),
            "base_sha": base_sha,
            "changed_paths": sorted(
                {path for record in selected for path in record.changed_paths}
            ),
            "verification": verification,
        }
