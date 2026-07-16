import ast
from pathlib import Path


EXPECTED_PRODUCTION = '''class EventBus:
    def __init__(self):
        self._listeners = {}

    def subscribe(self, event, callback):
        self._listeners.setdefault(event, []).append(callback)

    def unsubscribe(self, event, callback):
        listeners = self._listeners.get(event, [])
        if callback not in listeners:
            raise ValueError("callback is not subscribed")
        listeners.remove(callback)

    def publish(self, event, payload):
        return [callback(payload) for callback in tuple(self._listeners.get(event, []))]
'''


def test_production_module_was_not_changed():
    assert Path("event_bus.py").read_text(encoding="utf-8") == EXPECTED_PRODUCTION


def test_required_tests_exist_and_exercise_unsubscribe():
    source = Path("test_event_bus_contract.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    expected = {
        "test_unsubscribe_stops_future_notifications",
        "test_unsubscribe_unknown_callback_raises",
    }
    assert expected <= set(functions)
    for name in expected:
        calls = [
            node
            for node in ast.walk(functions[name])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "unsubscribe"
        ]
        assert calls, f"{name} must call EventBus.unsubscribe"
