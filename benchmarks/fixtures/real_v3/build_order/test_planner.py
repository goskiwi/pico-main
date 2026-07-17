from planner import build_order


def test_independent_tasks_keep_declaration_order():
    tasks = {"lint": (), "test": (), "package": ()}
    assert build_order(tasks) == ["lint", "test", "package"]


def test_empty_plan_has_no_steps():
    assert build_order({}) == []
