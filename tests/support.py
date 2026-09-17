import json
import shlex
import sys
from pathlib import Path

from pico import AssistantTurn, Pico, PicoConfig, SessionStore, Workspace
from pico.command_runner import CommandRunner


class HostShellRunner(CommandRunner):
    """Explicit test dependency for protocol tests, not a production fallback."""

    @property
    def execution_policy(self):
        return {"executor": "test-host", "cwd": ".", "environment_policy": "minimal"}

    def reconcile(self):
        pass

    def close(self):
        pass


class ScriptedModel:
    """Deterministic model double that still traverses the real Pico runtime."""

    model = "scripted-model"
    context_window_tokens = None
    input_limit_tokens = None

    def __init__(self, actions, *, before_action=None):
        self.actions = list(actions)
        self.before_action = before_action
        self.requests = []
        self.results = []
        self.result_batches = []
        self.last_completion_metadata = {}
        self._pending_call_ids = ()

    def reset_action_session(self):
        self._pending_call_ids = ()

    @staticmethod
    def estimate_action_tool_tokens(action_tools, count_tokens):
        return count_tokens(json.dumps(list(action_tools), sort_keys=True))

    def estimate_action_input_tokens(
        self, messages, *, system_prompt, action_tools, token_counter
    ):
        rendered = json.dumps(
            [message.to_dict() for message in messages],
            ensure_ascii=False,
            sort_keys=True,
        )
        return (
            token_counter(rendered)
            + token_counter(system_prompt)
            + self.estimate_action_tool_tokens(action_tools, token_counter)
        )

    def complete_turn(
        self,
        messages,
        max_output_tokens,
        *,
        system_prompt,
        action_tools,
        execution_context,
    ):
        execution_context.check_active()
        if self._pending_call_ids:
            raise RuntimeError("scripted calls have no recorded results")
        index = len(self.requests)
        if self.before_action is not None:
            self.before_action(index)
        if not self.actions:
            raise AssertionError("ScriptedModel has no action left")
        scripted = self.actions.pop(0)
        turn = (
            scripted
            if isinstance(scripted, AssistantTurn)
            else AssistantTurn(scripted)
        )
        action = turn.action
        self.requests.append(
            {
                "messages": tuple(messages),
                "system_prompt": system_prompt,
                "action_tools": tuple(action_tools),
                "max_output_tokens": max_output_tokens,
            }
        )
        self.last_completion_metadata = {
            "input_tokens": sum(len(message.text) for message in messages),
            "cached_tokens": 0,
            "output_tokens": 1,
            "reasoning_tokens": 0,
        }
        if action.kind == "tool":
            self._pending_call_ids = tuple(
                call.call_id for call in action.tool_calls
            )
        return turn

    def projected_context_tokens(
        self, results, *, system_prompt, action_tools, token_counter
    ):
        return (
            token_counter("\n".join(results))
            + token_counter(system_prompt)
            + self.estimate_action_tool_tokens(action_tools, token_counter)
        )

    def record_action_results(self, results):
        batch = tuple(str(item) for item in results)
        expected = len(self._pending_call_ids) if self._pending_call_ids else 1
        if len(batch) != expected:
            raise ValueError("scripted continuation has the wrong result count")
        self.result_batches.append(batch)
        self.results.extend(batch)
        self._pending_call_ids = ()


def assert_file_command(path, expected):
    source = (
        "from pathlib import Path; "
        f"assert Path({str(path)!r}).read_text(encoding='utf-8') == {expected!r}"
    )
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


def approve_all(_name, _args, _plan):
    return True


def request_text(request):
    return "\n".join(
        [request["system_prompt"], *(message.text for message in request["messages"])]
    )


def build_agent(
    root,
    actions,
    *,
    before_action=None,
    approval_handler=approve_all,
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
            context_limit_tokens=32_000,
            recent_history_tokens=2_000,
            max_output_tokens=1_000,
        ),
        approval_handler=approval_handler,
        shell_runner=HostShellRunner(root),
    )
    return agent, model
