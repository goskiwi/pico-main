from pico.verification import parse_verification_output


def test_pytest_output_is_structured_and_stably_signed():
    output = """collected 4 items
tests/test_a.py ...F
FAILED tests/test_a.py::test_four - AssertionError
1 failed, 3 passed in 0.21s
"""

    first = parse_verification_output("python -m pytest -q", output, 1)
    second = parse_verification_output("python -m pytest -q", output.replace("0.21s", "1.91s"), 1)

    assert first["verifier"] == "pytest"
    assert first["collected"] == 4
    assert first["passed"] == 3
    assert first["failed"] == 1
    assert first["failed_tests"] == ["tests/test_a.py::test_four"]
    assert first["failure_signature"] == second["failure_signature"]


def test_successful_verification_has_no_failure_signature():
    result = parse_verification_output("python -m pytest -q", "2 passed in 0.10s", 0)
    assert result["passed"] == 2
    assert result["failure_signature"] == ""


def test_double_quiet_pytest_progress_is_counted():
    result = parse_verification_output(
        "python -m pytest -q",
        "..                                                                       [100%]",
        0,
    )
    assert result["collected"] == 2
    assert result["passed"] == 2
