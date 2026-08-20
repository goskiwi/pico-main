from pico import (
    FakeModelClient,
    ModelAction,
    Pico,
    PicoConfig,
    SessionStore,
    WorkspaceContext,
)
from pico.hooks import HookDirective


def build_agent(tmp_path, outputs, hooks):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pico(
        FakeModelClient(outputs),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico" / "sessions"),
        config=PicoConfig(approval_policy="auto", verification_command=""),
        hooks=hooks,
    )


def test_before_tool_hook_can_only_further_restrict_execution(tmp_path):
    class BlockListFiles:
        def before_tool_call(self, context):
            if context.call.name == "list_files":
                return HookDirective(block=True, reason="host policy denied listing")
            return None

    agent = build_agent(tmp_path, [], [BlockListFiles()])
    outcome = agent.tools.run("list_files", {"path": "."})

    assert outcome.status == "rejected"
    assert outcome.execution_state == "not_started"
    assert outcome.failure.code == "hook_blocked"
    assert "host policy denied" in outcome.failure.detail


def test_after_tool_hook_adds_guidance_without_rewriting_outcome(tmp_path):
    class GuideAfterRead:
        def after_tool_result(self, context):
            assert context.outcome.status == "ok"
            return HookDirective(guidance="Inspect the caller next.")

    agent = build_agent(
        tmp_path,
        [
            ModelAction.tool("read_file", {"path": "README.md", "start": 1, "end": 1}),
            ModelAction.final("Done."),
        ],
        [GuideAfterRead()],
    )

    assert agent.ask("Inspect") == "Done."
    provider_result = agent.model_client.recorded_action_results[0][1]
    assert "demo" in provider_result
    assert "Inspect the caller next." in provider_result
    entries = agent.services.run_store.read_entries(agent.run.task_state)
    assert any(entry.kind == "policy_decided" for entry in entries)


def test_should_stop_after_turn_stops_after_completed_tool_result(tmp_path):
    class StopAfterTurn:
        def should_stop_after_turn(self, context):
            return HookDirective(stop=True, reason="host requested graceful stop")

    agent = build_agent(
        tmp_path,
        [ModelAction.tool("read_file", {"path": "README.md", "start": 1, "end": 1})],
        [StopAfterTurn()],
    )

    answer = agent.ask("Inspect")

    assert answer == "Stopped by runtime policy: host requested graceful stop."
    assert agent.run.task_state.stop_reason == "policy_stop"
