import ast
from pathlib import Path


def test_required_boundary_tests_were_added():
    candidate = Path("test_inventory_edge_cases.py")
    assert candidate.is_file(), "create test_inventory_edge_cases.py"
    tree = ast.parse(candidate.read_text(encoding="utf-8"))
    test_names = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "test_reserve_rejects_zero_quantity" in test_names
    assert "test_reserve_rejects_negative_quantity" in test_names

    raises_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "pytest"
        and node.func.attr == "raises"
    ]
    assert len(raises_calls) >= 2, "both tests must assert the ValueError contract"
