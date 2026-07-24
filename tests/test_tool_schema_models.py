import hashlib
import json

from pydantic import BaseModel

from pico.tools import (
    BASE_TOOL_SPECS,
    DELEGATE_MANY_TOOL_SPEC,
    DELEGATE_TOOL_SPEC,
    responses_action_tools,
)


LEGACY_SCHEMA_SHA256 = "a68b849e153c48c8aaf103594b0d08bf06e841f146819b7a4b18826d8b65619c"
RESPONSES_SCHEMA_SHA256 = "2951d28d8d6923fcb1fbf4977c4ed7c9ef495a3e291f4788e6e0bc715f840ef9"


def _registry():
    return {
        **{name: dict(spec) for name, spec in BASE_TOOL_SPECS.items()},
        "delegate": dict(DELEGATE_TOOL_SPEC),
        "delegate_many": dict(DELEGATE_MANY_TOOL_SPEC),
    }


def _digest(value):
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_tool_registry_derives_legacy_schemas_from_pydantic_models():
    registry = _registry()

    assert all(issubclass(tool["args_schema"], BaseModel) for tool in registry.values())
    legacy_registry = {
        name: {key: value for key, value in tool.items() if key != "args_schema"}
        for name, tool in registry.items()
    }
    assert _digest(legacy_registry) == LEGACY_SCHEMA_SHA256


def test_pydantic_models_preserve_strict_responses_schema():
    assert _digest(responses_action_tools(_registry())) == RESPONSES_SCHEMA_SHA256
