from pathlib import Path


def test_agent_changed_strategy_after_the_progress_nudge():
    recovery = Path("recovery.txt")
    assert recovery.exists()
    assert recovery.read_text(encoding="utf-8").strip() == "recovered"
