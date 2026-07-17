import pytest

from planner import build_order


def test_dependencies_precede_dependents():
    tasks = {"package": ("compile",), "compile": ("generate",), "generate": ()}
    assert build_order(tasks) == ["generate", "compile", "package"]


def test_ready_tasks_use_declaration_order():
    tasks = {
        "package": ("compile",),
        "lint": (),
        "compile": ("generate",),
        "generate": (),
    }
    assert build_order(tasks) == ["lint", "generate", "compile", "package"]


def test_shared_dependency_appears_once():
    tasks = {"left": ("base",), "right": ("base",), "base": ()}
    order = build_order(tasks)
    assert order.count("base") == 1
    assert order.index("base") < order.index("left")
    assert order.index("base") < order.index("right")


def test_unknown_dependency_raises_without_mutating_input():
    tasks = {"package": ["missing"]}
    before = {name: list(dependencies) for name, dependencies in tasks.items()}
    with pytest.raises(KeyError):
        build_order(tasks)
    assert tasks == before


def test_cycle_is_rejected():
    with pytest.raises(ValueError):
        build_order({"a": ("b",), "b": ("a",)})
