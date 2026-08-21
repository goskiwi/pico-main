"""Resume discovery from Session active_run_id and the Run Log tail."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .run_log import replay_events

if TYPE_CHECKING:
    from .runtime import Pico

RESUME_NONE = "no-active-run"
RESUME_READY = "resumable"
RESUME_MISSING = "run-log-missing"
RESUME_WORKSPACE_MISMATCH = "workspace-mismatch"


class RuntimeRecovery:
    def __init__(self, runtime: Pico):
        self.runtime = runtime
        self.state = {}

    def evaluate(self):
        runtime = self.runtime
        original_active_run_id = str(runtime.session.data.get("active_run_id", ""))
        active_run_id = original_active_run_id
        status = RESUME_NONE
        projection = None
        events = ()
        if runtime.session.data.get("workspace_root") != str(runtime.workspace.root):
            status = RESUME_WORKSPACE_MISMATCH
        else:
            if not active_run_id:
                active_run_id, events, projection = (
                    runtime.services.run_store.find_active_run(
                    runtime.session.data["id"]
                    )
                )
            if not active_run_id:
                self.state = {
                    "status": status,
                    "active_run_id": "",
                    "projection": None,
                    "events": (),
                }
                return self.state
            if not runtime.services.run_store.has_events(active_run_id):
                status = RESUME_MISSING
                active_run_id = ""
            else:
                if not events:
                    events = tuple(
                        runtime.services.run_store.read_events(active_run_id)
                    )
                    projection = replay_events(events)
                if projection.session_id != runtime.session.data["id"]:
                    status = RESUME_MISSING
                    active_run_id = ""
                elif projection.terminal:
                    status = RESUME_NONE
                    active_run_id = ""
                else:
                    status = RESUME_READY
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

    def render(self):
        projection = self.state.get("projection")
        if projection is None or self.state.get("status") != RESUME_READY:
            return "Run recovery:\n- no active Run Log"
        pending = projection.summary()["pending_operations"]
        return "\n".join(
            [
                "Run recovery:",
                f"- status: {self.state['status']}",
                f"- run: {projection.run_id}",
                f"- goal: {projection.user_request or '-'}",
                f"- last tool: {projection.last_executed_tool or '-'}",
                f"- pending tools: {', '.join(pending) or '-'}",
            ]
        )
