"""Resume discovery from Session active_run_id and the Run Log tail."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime import Pico

RESUME_NONE = "no-active-run"
RESUME_READY = "resumable"


class RuntimeRecovery:
    def __init__(self, runtime: Pico):
        self.runtime = runtime
        self.state = {}

    def evaluate(self):
        runtime = self.runtime
        original_active_run_id = str(runtime.session.data.get("active_run_id", ""))
        active_run_id = original_active_run_id
        projection = None
        events = ()
        if runtime.session.data.get("workspace_root") == str(runtime.workspace.root):
            if not active_run_id:
                active_run_id, events, projection = (
                    runtime.dependencies.run_store.find_active_run(
                    runtime.session.data["id"]
                    )
                )
            if (
                active_run_id
                and not events
                and runtime.dependencies.run_store.has_events(active_run_id)
            ):
                events, projection = runtime.dependencies.run_store.load_run(
                    active_run_id
                )
        resumable = bool(
            projection is not None
            and projection.session_id == runtime.session.data["id"]
            and not projection.terminal
        )
        status = RESUME_READY if resumable else RESUME_NONE
        persisted_active_run_id = active_run_id if status == RESUME_READY else ""
        if persisted_active_run_id != original_active_run_id:
            runtime.session.set_active_run(persisted_active_run_id)
        self.state = {
            "status": status,
            "active_run_id": persisted_active_run_id,
            "projection": projection,
            "events": events if status == RESUME_READY else (),
        }
        return self.state
