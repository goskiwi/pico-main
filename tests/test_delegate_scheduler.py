import threading
import time
from types import SimpleNamespace

from pico.delegate_scheduler import DelegateScheduler
import pico.delegate_scheduler as delegate_scheduler


def test_scheduler_runs_children_concurrently_and_preserves_spec_order(monkeypatch):
    monkeypatch.setattr(delegate_scheduler, "DELEGATE_MAX_CONCURRENCY", 2)
    monkeypatch.setattr(delegate_scheduler, "DELEGATE_TOTAL_STEP_BUDGET", 12)
    monkeypatch.setattr(delegate_scheduler, "DELEGATE_BATCH_TIMEOUT_SECONDS", 1.0)
    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_run_delegate_child(agent, spec):
        del agent
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return {"role": spec["role"], "answer": spec["task"]}

    started_at = time.monotonic()
    outcomes = DelegateScheduler(
        SimpleNamespace(), child_runner=fake_run_delegate_child
    ).run(
        [
            {"role": "explore", "task": "one", "max_steps": 2},
            {"role": "review", "task": "two", "max_steps": 2},
            {"role": "verify", "task": "three", "max_steps": 2},
        ]
    )
    elapsed = time.monotonic() - started_at

    assert [outcome.index for outcome in outcomes] == [1, 2, 3]
    assert [outcome.status for outcome in outcomes] == ["ok", "ok", "ok"]
    assert [outcome.result["answer"] for outcome in outcomes] == ["one", "two", "three"]
    assert max_active == 2
    assert elapsed < 0.13


def test_scheduler_marks_specs_that_exceed_the_shared_step_budget(monkeypatch):
    monkeypatch.setattr(delegate_scheduler, "DELEGATE_TOTAL_STEP_BUDGET", 3)
    called_roles = []

    def fake_run_delegate_child(agent, spec):
        del agent
        called_roles.append(spec["role"])
        return {"role": spec["role"], "answer": "done"}

    scheduler = DelegateScheduler(SimpleNamespace(), child_runner=fake_run_delegate_child)
    outcomes = scheduler.run(
        [
            {"role": "explore", "task": "one", "max_steps": 2},
            {"role": "review", "task": "two", "max_steps": 1},
            {"role": "verify", "task": "three", "max_steps": 1},
        ]
    )

    assert [outcome.status for outcome in outcomes] == ["ok", "ok", "budget_exhausted"]
    assert outcomes[2].reserved_steps == 0
    assert "remaining 0" in outcomes[2].error
    assert scheduler.last_reserved_steps == 3
    assert set(called_roles) == {"explore", "review"}


def test_scheduler_reports_child_errors():
    def fake_run_delegate_child(agent, spec):
        del agent
        if spec["role"] == "review":
            raise RuntimeError("expected child failure")
        return {
            "role": spec["role"],
            "answer": "deterministic child result",
        }

    outcomes = DelegateScheduler(SimpleNamespace(), child_runner=fake_run_delegate_child).run(
        [
            {"role": "explore", "task": "one", "max_steps": 1},
            {"role": "review", "task": "two", "max_steps": 1},
        ]
    )

    assert outcomes[0].status == "ok"
    assert outcomes[0].result["answer"] == "deterministic child result"
    assert outcomes[1].status == "error"
    assert "RuntimeError: expected child failure" == outcomes[1].error


def test_scheduler_marks_a_stopped_child_as_an_error():
    def stopped_child(agent, spec):
        del agent
        return {
            "role": spec["role"],
            "answer": "Stopped after reaching the step limit.",
            "status": "stopped",
            "stop_reason": "step_limit_reached",
        }

    outcome = DelegateScheduler(SimpleNamespace(), child_runner=stopped_child).run(
        [{"role": "explore", "task": "inspect", "max_steps": 1}]
    )[0]

    assert outcome.status == "error"
    assert outcome.error == "child stopped with status=stopped (step_limit_reached)"


def test_scheduler_returns_timeout_outcomes_without_waiting_for_slow_children(monkeypatch):
    monkeypatch.setattr(delegate_scheduler, "DELEGATE_BATCH_TIMEOUT_SECONDS", 0.01)

    def slow_child(agent, spec):
        del agent, spec
        time.sleep(0.08)
        return {"role": "explore", "answer": "late"}

    started_at = time.monotonic()
    outcomes = DelegateScheduler(SimpleNamespace(), child_runner=slow_child).run(
        [{"role": "explore", "task": "slow", "max_steps": 1}]
    )
    elapsed = time.monotonic() - started_at

    assert outcomes[0].status == "timeout"
    assert "0.01s timeout" in outcomes[0].error
    assert elapsed < 0.05
