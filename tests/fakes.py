"""Deterministic model doubles for offline tests."""

import json

from pico.actions import ModelAction


SCRIPTED_PROTOCOL = "scripted_action"


def tool_action_json(payload):
    data = json.loads(payload)
    return ModelAction.tool(
        data["name"],
        data.get("args", {}),
        protocol=SCRIPTED_PROTOCOL,
    )


def final_action(answer):
    return ModelAction.final(answer, protocol=SCRIPTED_PROTOCOL)


def retry_action(error, *, raw_preview=""):
    return ModelAction.retry(
        error,
        protocol=SCRIPTED_PROTOCOL,
        raw_preview=raw_preview,
    )


class FakeModelClient:
    """Return scripted structured actions or raw auxiliary-model completions."""

    model = "fake"
    base_url = "test://fake"
    supports_prompt_cache = False

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        del max_new_tokens, kwargs
        self.prompts.append(prompt)
        if not self.last_completion_metadata:
            self.last_completion_metadata = {}
        if not self.outputs:
            raise RuntimeError("fake model ran out of outputs")
        return self.outputs.pop(0)

    def complete_action(self, prompt, max_new_tokens, **kwargs):
        kwargs.pop("action_tools", None)
        output = self.complete(prompt, max_new_tokens, **kwargs)
        if isinstance(output, ModelAction):
            return output
        raise TypeError("scripted action must be a ModelAction")

    def reset_action_session(self):
        return None

    def record_action_result(self, action, result):
        del action, result
