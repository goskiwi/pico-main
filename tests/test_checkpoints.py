import pico.checkpoints as checkpoints
from pico.task_state import STATUS_COMPLETED, TaskState
from tests.helpers import build_agent


def test_infer_next_step_from_task_state():
    completed = TaskState(task_id="task_1", run_id="run_1", user_request="done", status=STATUS_COMPLETED)
    limited = TaskState(
        task_id="task_2",
        run_id="run_2",
        user_request="continue",
        status="running",
        stop_reason="step_limit_reached",
    )
    after_tool = TaskState(task_id="task_3", run_id="run_3", user_request="inspect", status="running", last_tool="read_file")

    assert checkpoints.infer_next_step(completed) == "No next step recorded."
    assert checkpoints.infer_next_step(limited) == "Resume from the latest checkpoint and continue the task."
    assert checkpoints.infer_next_step(after_tool) == "Decide the next action after read_file."


def test_empty_checkpoint_state_renders_no_checkpoint_text(tmp_path):
    agent = build_agent(tmp_path, [])

    assert checkpoints.current_checkpoint(agent) is None
    assert checkpoints.render_checkpoint_text(agent) == ""
    assert checkpoints.evaluate_resume_state(agent)["status"] == checkpoints.CHECKPOINT_NONE_STATUS
