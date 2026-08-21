"""In-memory projection of one Run Log."""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from .features.memory import WorkingState

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_STOPPED = "stopped"
STATUS_FAILED = "failed"
TASK_STATUSES = frozenset(
    {STATUS_RUNNING, STATUS_COMPLETED, STATUS_STOPPED, STATUS_FAILED}
)

STOP_REASON_FINAL_ANSWER_RETURNED = "final_answer_returned"
STOP_REASON_TOOL_EXECUTION_LIMIT = "tool_execution_limit"
STOP_REASON_INVALID_OUTPUT_LIMIT = "invalid_output_limit"
STOP_REASON_MODEL_ERROR = "model_error"
STOP_REASON_TOOL_TIMEOUT = "tool_timeout"
STOP_REASON_APPROVAL_DENIED = "approval_denied"
STOP_REASON_PERSISTENCE_ERROR = "persistence_error"
STOP_REASON_RESUME_LOAD_ERROR = "resume_load_error"


@dataclass
class TaskState:
    run_id: str
    task_id: str
    working_state: WorkingState
    status: str = STATUS_RUNNING
    executed_tool_count: int = 0
    model_request_count: int = 0
    last_executed_tool: str = ""
    stop_reason: str = ""
    final_answer: str = ""

    @classmethod
    def create(cls, task_id, user_request, run_id=""):
        if not run_id:
            run_id = "run_" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
        return cls(
            run_id=run_id,
            task_id=task_id,
            working_state=WorkingState(goal=user_request),
        ).validate()

    @classmethod
    def from_dict(cls, data):
        if "user_request" in data:
            raise ValueError("task state uses working_state.goal, not user_request")
        if "working_state" not in data:
            raise ValueError("task state requires working_state")
        working_state = WorkingState.from_dict(data["working_state"])
        return cls(
            run_id=str(data.get("run_id", "")),
            task_id=str(data.get("task_id", "")),
            working_state=working_state,
            status=str(data.get("status", STATUS_RUNNING)),
            executed_tool_count=int(data.get("executed_tool_count", 0)),
            model_request_count=int(data.get("model_request_count", 0)),
            last_executed_tool=str(data.get("last_executed_tool", "")),
            stop_reason=str(data.get("stop_reason", "")),
            final_answer=str(data.get("final_answer", "")),
        ).validate()

    @property
    def user_request(self):
        return self.working_state.goal

    def validate(self):
        WorkingState.from_dict(self.working_state.to_dict())
        if self.status not in TASK_STATUSES:
            raise ValueError(f"invalid task status: {self.status}")
        if self.model_request_count < 0:
            raise ValueError("model_request_count cannot be negative")
        if self.executed_tool_count < 0:
            raise ValueError("executed_tool_count cannot be negative")
        if self.status == STATUS_RUNNING:
            if self.stop_reason:
                raise ValueError("running task cannot have stop_reason")
            if self.final_answer:
                raise ValueError("running task cannot have final_answer")
        elif self.status == STATUS_COMPLETED:
            if self.stop_reason != STOP_REASON_FINAL_ANSWER_RETURNED:
                raise ValueError(
                    "completed task requires final_answer_returned stop_reason"
                )
            if not self.final_answer.strip():
                raise ValueError("completed task requires final_answer")
        elif not self.stop_reason:
            raise ValueError(f"{self.status} task requires stop_reason")
        if self.executed_tool_count == 0 and self.last_executed_tool:
            raise ValueError(
                "task without tool executions cannot have last_executed_tool"
            )
        if self.executed_tool_count > 0 and not self.last_executed_tool:
            raise ValueError("executed tools require last_executed_tool")
        return self

    def apply_event(self, event):
        apply_task_event(self, event)
        return self.validate()

    def to_dict(self):
        self.validate()
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "working_state": self.working_state.to_dict(),
            "status": self.status,
            "executed_tool_count": self.executed_tool_count,
            "model_request_count": self.model_request_count,
            "last_executed_tool": self.last_executed_tool,
            "stop_reason": self.stop_reason,
            "final_answer": self.final_answer,
        }


def apply_task_event(state, event):
    """Apply one durable Run event to a task-shaped projection."""

    payload = dict(event.payload)
    state.run_id = str(event.run_id)
    state.task_id = str(event.task_id or state.task_id)
    state.working_state.apply_event(event)

    if event.kind == "model_requested":
        state.model_request_count += 1
    elif event.kind == "tool_result":
        outcome = dict(payload.get("outcome", {}) or {})
        tool_name = str(
            outcome.get("tool_name", payload.get("tool_name", ""))
        )
        if tool_name and outcome.get("execution_state") != "not_started":
            state.executed_tool_count += 1
            state.last_executed_tool = tool_name
    elif event.kind == "assistant_final":
        state.status = STATUS_COMPLETED
        state.stop_reason = str(payload["stop_reason"])
        state.final_answer = str(payload.get("content", ""))
    elif event.kind == "run_stopped":
        state.status = STATUS_STOPPED
        state.stop_reason = str(payload.get("stop_reason", ""))
        state.final_answer = str(payload.get("content", ""))
    return state
