"""Day 7: run one complete coding task across Pico's core runtime."""

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
from pico.mutations import file_revision
from pico.sandbox import SandboxResult


class RecordingVerificationSandbox:
    def __init__(self, target):
        self.target = Path(target)
        self.calls = []

    def run(self, argv, **kwargs):
        content = self.target.read_text(encoding="utf-8")
        passed = "return left + right" in content
        self.calls.append(
            {
                "argv": list(argv),
                "workspace_contains_fix": passed,
                "timeout": kwargs.get("timeout"),
            }
        )
        return SandboxResult(
            returncode=0 if passed else 1,
            stdout="1 passed\n" if passed else "1 failed\n",
            cleanup_state="completed",
        )


def print_section(title, value):
    print(f"\n=== {title} ===")
    print(json.dumps(value, indent=2, ensure_ascii=False))


def main():
    with tempfile.TemporaryDirectory(prefix="pico-day7-") as directory:
        root = Path(directory)
        target = root / "calculator.py"
        target.write_text(
            "def add(left, right):\n"
            "    \"\"\"Return the sum of two numbers.\"\"\"\n"
            "    return left - right\n",
            encoding="utf-8",
        )
        (root / "test_calculator.py").write_text(
            "from calculator import add\n\n"
            "def test_add():\n"
            "    assert add(2, 3) == 5\n",
            encoding="utf-8",
        )
        initial_revision = file_revision(target)
        verify_command = "python -m pytest -q"
        sandbox = RecordingVerificationSandbox(target)
        model = FakeModelClient(
            [
                ModelAction.tool(
                    "update_working_state",
                    {
                        "add_constraints": ["Only edit calculator.py"],
                        "add_next_steps": ["Inspect and fix add, then verify"],
                    },
                    call_id="call_plan",
                ),
                ModelAction.tool(
                    "read_file",
                    {
                        "path": "calculator.py",
                        "start_line": 1,
                        "end_line": 40,
                    },
                    call_id="call_read",
                ),
                ModelAction.tool(
                    "edit_file",
                    {
                        "path": "calculator.py",
                        "old_text": "return left - right",
                        "new_text": "return left + right",
                        "expected_revision": initial_revision,
                    },
                    call_id="call_edit",
                ),
                ModelAction.tool(
                    "memory_store",
                    {
                        "action": "create",
                        "filename": "reference_calculator_verification.md",
                        "name": "Calculator verification",
                        "description": "Command used to verify calculator changes.",
                        "memory_type": "reference",
                        "content": verify_command,
                        "why": "",
                        "how_to_apply": "",
                        "expires_at": "",
                    },
                    call_id="call_memory",
                ),
                ModelAction.tool(
                    "update_working_state",
                    {
                        "add_decisions": [
                            "add now returns left + right"
                        ],
                        "remove_next_steps": [
                            "Inspect and fix add, then verify"
                        ],
                    },
                    call_id="call_finish_state",
                ),
                ModelAction.final("Fixed calculator.add and verified the change."),
            ]
        )
        agent = Pico(
            model_client=model,
            workspace=WorkspaceContext.build(root),
            session_store=SessionStore(root / ".pico" / "sessions"),
            config=PicoConfig(
                approval_policy="auto",
                verification_command=verify_command,
            ),
            sandbox=sandbox,
        )

        answer = agent.ask(
            "Fix calculator.add so the existing addition test passes",
            task_kind="modify",
            requires_workspace_change=True,
            requires_verification=True,
        )
        run_id = agent.run.projection.run_id
        events = agent.dependencies.run_store.read_events(run_id)
        replayed = agent.dependencies.run_store.replay(run_id)
        evidence = RunEvidence.from_events(events)
        calls = {
            event.call_id: event
            for event in events
            if event.kind == "assistant_tool_call"
        }
        results = {
            event.call_id: event
            for event in events
            if event.kind == "tool_result"
        }
        transactions = [
            {
                "call_id": call_id,
                "tool": call.name,
                "status": results[call_id].outcome_status,
                "side_effect_state": results[call_id].side_effect_state,
                "affected_paths": list(results[call_id].affected_paths),
            }
            for call_id, call in calls.items()
        ]
        verification_events = [
            event.payload
            for event in events
            if event.kind == "verification_result"
        ]
        turn_reuse = [
            event.payload["prompt_reused"]
            for event in events
            if event.kind == "turn_metrics"
        ]
        persisted_files = sorted(
            path.relative_to(root).as_posix()
            for path in (root / ".pico").rglob("*")
            if path.is_file()
        )

        assert answer == "Fixed calculator.add and verified the change."
        assert "return left + right" in target.read_text(encoding="utf-8")
        assert replayed.status == "completed"
        assert replayed.task.working.next_steps == ()
        assert evidence.changed_paths == ["calculator.py"]
        assert evidence.latest_verification_for_state(
            evidence.last_workspace_mutation_sequence,
            verification_events[-1]["finished_changed_path_states"],
        ) is not None
        assert len(sandbox.calls) == 1
        assert "source_tool_call_id" not in verification_events[0]
        assert "calculator.py" in model.prompts[0]
        assert (
            root
            / ".pico"
            / "memory"
            / "cards"
            / "reference_calculator_verification.md"
        ).is_file()

        print_section(
            "最终结果",
            {
                "answer": answer,
                "task_status": replayed.status,
                "calculator.py": target.read_text(encoding="utf-8"),
                "working_state": replayed.task.working.to_dict(),
            },
        )
        print_section(
            "完整工具事务",
            {
                "transactions": transactions,
                "verification_events": verification_events,
                "sandbox_calls": sandbox.calls,
                "verification_was_run_once_by_completion_gate": len(sandbox.calls)
                == 1,
            },
        )
        print_section(
            "上下文与持久化",
            {
                "prompt_reused_by_turn": turn_reuse,
                "repo_map_in_initial_prompt": "calculator.py"
                in model.prompts[0],
                "changed_paths_from_evidence": evidence.changed_paths,
                "persisted_files": persisted_files,
            },
        )


if __name__ == "__main__":
    main()
