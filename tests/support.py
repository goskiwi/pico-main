import json
import shlex
import sys
from pathlib import Path

from pico import Pico, PicoConfig, SessionStore, Workspace


class ScriptedModel:
    """Deterministic model double that still traverses the real Pico runtime."""

    model = "scripted-model"

    def __init__(self, actions, *, before_action=None):
        self.actions = list(actions)
        self.before_action = before_action
        self.requests = []
        self.results = []
        self.last_completion_metadata = {}
        self._pending_call_id = ""

    def reset_action_session(self):
        self._pending_call_id = ""

    @staticmethod
    def estimate_action_tool_tokens(action_tools, count_tokens):
        return count_tokens(json.dumps(list(action_tools), sort_keys=True))

    def estimate_action_input_tokens(
        self, input_text, *, instructions, action_tools, token_counter
    ):
        return token_counter(input_text) + token_counter(instructions) + self.estimate_action_tool_tokens(
            action_tools, token_counter
        )

    def complete_action(
        self,
        input_text,
        max_new_tokens,
        *,
        instructions,
        action_tools,
        execution_context,
    ):
        execution_context.check_active()
        if self._pending_call_id:
            raise RuntimeError("scripted call has no recorded result")
        index = len(self.requests)
        if self.before_action is not None:
            self.before_action(index)
        if not self.actions:
            raise AssertionError("ScriptedModel has no action left")
        action = self.actions.pop(0)
        self.requests.append(
            {
                "input_text": input_text,
                "instructions": instructions,
                "action_tools": tuple(action_tools),
                "max_new_tokens": max_new_tokens,
            }
        )
        self.last_completion_metadata = {
            "input_tokens": len(input_text),
            "cached_tokens": 0,
            "output_tokens": 1,
        }
        if action.kind == "tool":
            self._pending_call_id = action.tool_call.call_id
        return action

    def projected_context_tokens(
        self, results, *, instructions, action_tools, token_counter
    ):
        return self.estimate_action_input_tokens(
            "\n".join(results),
            instructions=instructions,
            action_tools=action_tools,
            token_counter=token_counter,
        )

    def record_action_results(self, results):
        self.results.extend(str(item) for item in results)
        self._pending_call_id = ""


def verification_command(path, expected):
    source = (
        "from pathlib import Path; "
        f"assert Path({str(path)!r}).read_text(encoding='utf-8') == {expected!r}"
    )
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


def build_agent(
    root,
    actions,
    *,
    verification="",
    verification_required=None,
    before_action=None,
):
    root = Path(root)
    workspace = Workspace.build(root, repo_root_override=root)
    store = SessionStore(root / ".pico" / "sessions")
    model = ScriptedModel(actions, before_action=before_action)
    agent = Pico(
        model,
        workspace,
        session=store.create(workspace.root),
        config=PicoConfig(
            mode="auto",
            verification_command=verification,
            verification_required=verification_required,
            context_budget_tokens=32_000,
            compaction_reserve_tokens=4_000,
            compaction_keep_recent_tokens=2_000,
            max_new_tokens=1_000,
        ),
    )
    return agent, model
