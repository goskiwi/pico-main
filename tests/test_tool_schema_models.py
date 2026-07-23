import hashlib
import json

from pydantic import BaseModel

from pico.tools import (
    BASE_TOOL_SPECS,
    DELEGATE_MANY_TOOL_SPEC,
    DELEGATE_TOOL_SPEC,
    responses_action_tools,
)


LEGACY_SCHEMA_SHA256 = "6ff1fa6c20d2cbf0ae82afa633a647d5d5c088f69d3a4a9aeef54f4e4c04fced"
RESPONSES_SCHEMA_SHA256 = "e7a97cdba3fbc09d655cd510fb9f2532c5b525d60062c521638cef358bb1de5d"


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
