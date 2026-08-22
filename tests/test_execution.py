import time

import pytest

from pico.execution import (
    CancellationToken,
    ExecutionBudget,
    ExecutionCancelled,
    ExecutionContext,
    ExecutionDeadlineExceeded,
)


def test_child_execution_shares_deadline_and_cancellation():
    token = CancellationToken()
    root = ExecutionContext.root(max_seconds=30, token=token)
    child = root.child()

    assert child.execution_id != root.execution_id
    assert child.deadline == root.deadline
    assert child.token is root.token
    assert 0 < child.bounded_timeout(10) <= 10

    root.request_stop("user_cancelled")

    with pytest.raises(ExecutionCancelled, match="user_cancelled"):
        child.check_active()


def test_expired_execution_rejects_new_work():
    context = ExecutionContext.root(
        max_seconds=1,
        deadline=time.monotonic() - 1,
    )

    with pytest.raises(ExecutionDeadlineExceeded, match="deadline exceeded"):
        context.bounded_timeout()


def test_execution_budget_only_contains_monitor_inputs():
    budget = ExecutionBudget(
        deadline=time.monotonic() + 10,
        max_output_bytes=1024,
    )

    assert budget.max_output_bytes == 1024

    with pytest.raises(ValueError, match="at least 1024"):
        ExecutionBudget(deadline=time.monotonic() + 10, max_output_bytes=100)
