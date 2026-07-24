from pathlib import Path


def test_agent_recorded_the_exact_tail_failure_node():
    answer = Path("answer.txt")
    assert answer.exists()
    assert answer.read_text(encoding="utf-8").strip() == (
        "tests/test_noisy_failure.py::test_tail_only_failure"
    )
