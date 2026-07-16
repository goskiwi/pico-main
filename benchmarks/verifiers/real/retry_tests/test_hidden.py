import ast
from pathlib import Path


def test_required_retry_boundary_tests_were_added():
    candidate = Path("test_retry_edge_cases.py")
    assert candidate.is_file(), "create test_retry_edge_cases.py"
    tree = ast.parse(candidate.read_text(encoding="utf-8"))
    test_names = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "test_zero_retries_calls_operation_once" in test_names
    assert "test_negative_retries_raise_value_error" in test_names
    assert any(
        isinstance(node, ast.Assert)
        for node in ast.walk(tree)
    ), "the zero-retry test must assert the call count"
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "pytest"
        and node.func.attr == "raises"
        for node in ast.walk(tree)
    ), "the negative-retry test must assert ValueError"
