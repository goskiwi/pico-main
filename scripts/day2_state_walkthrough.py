"""Day 2: prove that Run Log events rebuild Pico's in-memory state."""

import json
import tempfile
from pathlib import Path

from pico import (
    FakeModelClient,
    ModelAction,
    Pico,
    PicoConfig,
    SessionStore,
    WorkspaceContext,
)
from pico.evidence import RunEvidence


def print_section(title, value):
    print(f"\n=== {title} ===")
    print(json.dumps(value, indent=2, ensure_ascii=False))


def event_rows(events):
    return [
        {
            "sequence": event.sequence,
            "kind": event.kind,
            "tool": event.name,
            "status": event.outcome_status,
        }
        for event in events
    ]


def main():
    with tempfile.TemporaryDirectory(prefix="pico-day2-") as directory:
        root = Path(directory)
        (root / "README.md").write_text(
            "# State demo\n\nThe durable Run Log owns process facts.\n",
            encoding="utf-8",
        )
        model = FakeModelClient(
            [
                ModelAction.tool(
                    "update_working_state",
                    {
                        "add_constraints": ["Do not modify the workspace"],
                        "add_decisions": ["Inspect README through a file tool"],
                        "add_next_steps": ["Read README.md"],
                    },
                    call_id="call_working_state",
                ),
                ModelAction.tool(
                    "read_file",
                    {"path": "README.md", "start_line": 1, "end_line": 20},
                    call_id="call_readme",
                ),
                ModelAction.final("State walkthrough completed."),
            ]
        )
        store = SessionStore(root / ".pico" / "sessions")
        agent = Pico(
            model_client=model,
            workspace=WorkspaceContext.build(root),
            session_store=store,
            config=PicoConfig(
                approval_policy="auto",
                verification_command="",
            ),
        )

        answer = agent.ask("Inspect README without changing the workspace")
        run_id = agent.run.task_state.run_id
        events = agent.dependencies.run_store.read_events(run_id)
        replayed = agent.dependencies.run_store.replay(run_id)
        replayed_task = replayed.task_state()
        live_task = agent.run.task_state.to_dict()
        replayed_evidence = RunEvidence.from_events(events)
        session_on_disk = store.load(agent.session.data["id"])
        persisted_files = sorted(
            path.relative_to(root).as_posix()
            for path in (root / ".pico").rglob("*")
            if path.is_file()
        )

        assert [event.sequence for event in events] == list(
            range(1, len(events) + 1)
        )
        assert replayed_task == live_task
        assert replayed_evidence.effects == agent.run.evidence.effects
        assert session_on_disk["active_run_id"] == ""
        assert replayed.terminal is True

        print_section("最终回答", answer)
        print_section("持久化事件序列", event_rows(events))
        print_section(
            "Live TaskState 与 replay 结果",
            {
                "equal": live_task == replayed_task,
                "live": live_task,
                "replayed": replayed_task,
            },
        )
        print_section(
            "状态所有权",
            {
                "run_process_facts": f".pico/runs/{run_id}/events.jsonl",
                "session_pointer_after_completion": session_on_disk[
                    "active_run_id"
                ],
                "workspace_content_owner": "README.md",
                "derived_evidence": replayed_evidence.effects,
                "persisted_files": persisted_files,
            },
        )


if __name__ == "__main__":
    main()
