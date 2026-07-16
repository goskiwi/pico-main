import pytest

from retry import retry


def test_failure_uses_exactly_the_attempt_budget():
    calls = 0

    def fail():
        nonlocal calls
        calls += 1
        raise RuntimeError("still failing")

    with pytest.raises(RuntimeError, match="still failing"):
        retry(fail, max_attempts=3)
    assert calls == 3


def test_success_on_final_allowed_attempt_is_returned():
    calls = 0

    def eventually_succeeds():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("not yet")
        return "ok"

    assert retry(eventually_succeeds, max_attempts=3) == "ok"
    assert calls == 3
