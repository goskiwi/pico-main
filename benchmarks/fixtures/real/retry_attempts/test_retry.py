from retry import retry


def test_retry_returns_first_success():
    assert retry(lambda: "ok", max_attempts=1) == "ok"
