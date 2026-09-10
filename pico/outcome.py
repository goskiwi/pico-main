"""Public result returned by one Pico task run."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RunOutcome:
    session_id: str
    status: str
    answer: str
    run_id: str = ""
    stop_reason: str = ""
    verification: str = "not_run"
    turns: int = 0
    tools: int = 0
    metrics: dict = field(default_factory=dict)
    task_diff: dict = field(default_factory=dict)

    @property
    def terminal(self):
        return self.status in {"completed", "stopped"}

    @property
    def changed_paths(self):
        return tuple(self.task_diff.get("changed_paths", ()))

    def to_dict(self):
        return {
            "session_id": self.session_id,
            "status": self.status,
            "answer": self.answer,
            "run_id": self.run_id,
            "stop_reason": self.stop_reason,
            "verification": self.verification,
            "turns": self.turns,
            "tools": self.tools,
            "metrics": dict(self.metrics),
            "task_diff": dict(self.task_diff),
        }
