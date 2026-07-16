from retry import retry


def test_retry_returns_success():
    assert retry(lambda: "ok", retries=2) == "ok"
