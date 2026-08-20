"""Resume discovery from Session active_run_id and the Run Journal tail."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .run_journal import replay_entries

if TYPE_CHECKING:
    from .runtime import Pico

RESUME_NONE = "no-active-run"
RESUME_READY = "resumable"
RESUME_MISSING = "journal-missing"
RESUME_WORKSPACE_MISMATCH = "workspace-mismatch"


class RuntimeRecovery:
    def __init__(self, runtime: Pico):
        self.runtime = runtime
        self.state = {}

    def evaluate(self):
        runtime = self.runtime
        active_run_id = str(runtime.session.data.get("active_run_id", ""))
        status = RESUME_NONE
        projection = None
        entries = ()
        if runtime.session.data.get("workspace_root") != str(runtime.workspace.root):
            status = RESUME_WORKSPACE_MISMATCH
        else:
            if not active_run_id:
                active_run_id, entries, projection = (
                    runtime.services.run_store.find_active_run(
                    runtime.session.data["id"]
                    )
                )
                if active_run_id:
                    runtime.session.data["active_run_id"] = active_run_id
            if not active_run_id:
                self.state = {
                    "status": status,
                    "active_run_id": "",
                    "projection": None,
                    "entries": (),
                }
                return self.state
            if not runtime.services.run_store.has_journal(active_run_id):
                status = RESUME_MISSING
                runtime.session.data["active_run_id"] = ""
            else:
                if not entries:
                    entries = tuple(
                        runtime.services.run_store.read_entries(active_run_id)
                    )
                    projection = replay_entries(entries)
                if projection.session_id != runtime.session.data["id"]:
                    status = RESUME_MISSING
                    runtime.session.data["active_run_id"] = ""
                elif projection.terminal:
                    status = RESUME_NONE
                    runtime.session.data["active_run_id"] = ""
                else:
                    status = RESUME_READY
        self.state = {
            "status": status,
            "active_run_id": active_run_id if status == RESUME_READY else "",
            "projection": projection,
            "entries": entries if status == RESUME_READY else (),
        }
        return self.state

    def render(self):
        projection = self.state.get("projection")
        if projection is None or self.state.get("status") != RESUME_READY:
            return "Run recovery:\n- no active Run Journal"
        pending = projection.summary()["pending_operations"]
        return "\n".join(
            [
                "Run recovery:",
                f"- status: {self.state['status']}",
                f"- run: {projection.run_id}",
                f"- goal: {projection.user_request or '-'}",
                f"- last tool: {projection.last_tool or '-'}",
                f"- pending tools: {', '.join(pending) or '-'}",
            ]
        )
