import json

from pico.models import FakeModelClient
from pico.runtime import Pico
from pico.session_store import SessionStore
from tests.helpers import build_agent, build_workspace


def test_agent_creates_checkpoint_when_context_reduction_happens_and_artifacts_only_reference_it(tmp_path):
    agent = build_agent(tmp_path, ["<final>Done after checkpoint.</final>"])
    for index in range(10):
        agent.record(
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"history-{index}-" + ("A" * 260),
                "created_at": f"2026-04-07T10:{index:02d}:00+00:00",
            }
        )
    agent.memory.append_note("checkpoint note " + ("B" * 220), tags=("checkpoint",), created_at="2026-04-07T11:00:00+00:00")
    agent.context_manager.total_budget = 900
    agent.context_manager.section_budgets = {
        "prefix": 120,
        "memory": 120,
        "relevant_memory": 120,
        "history": 160,
    }

    assert agent.ask("Resume the long task") == "Done after checkpoint."

    checkpoint_state = agent.session["checkpoints"]
    checkpoint = checkpoint_state["items"][checkpoint_state["current_id"]]
    assert checkpoint["checkpoint_id"] == checkpoint_state["current_id"]
    assert checkpoint["schema_version"] == "phase1-v1"
    assert checkpoint["current_goal"] == "Resume the long task"
    assert checkpoint["key_files"] == []
    assert checkpoint["current_blocker"] == ""
    assert checkpoint["next_step"]

    task_state = json.loads(agent.run_store.task_state_path(agent.current_task_state).read_text(encoding="utf-8"))
    report = json.loads(agent.run_store.report_path(agent.current_task_state).read_text(encoding="utf-8"))
    trace_events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
    ]

    assert task_state["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert report["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert report["task_state"]["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert "current_goal" not in task_state
    assert "current_goal" not in report
    checkpoint_events = [event for event in trace_events if event["event"] == "checkpoint_created"]
    assert checkpoint_events
    assert checkpoint_events[-1]["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert "current_goal" not in checkpoint_events[-1]


def test_resume_prompt_uses_checkpoint_state_not_just_history(tmp_path):
    agent = build_agent(tmp_path, ["<final>checkpoint ready.</final>"])
    agent.session["checkpoints"] = {
        "current_id": "ckpt_manual",
        "items": {
            "ckpt_manual": {
                "checkpoint_id": "ckpt_manual",
                "parent_checkpoint_id": "",
                "schema_version": "phase1-v1",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Fix failing resume flow",
                "completed": ["Read runtime.py"],
                "excluded": ["Do not add branch summary"],
                "current_blocker": "Need to re-anchor stale file facts",
                "next_step": "Re-read runtime.py and refresh the checkpoint",
                "key_files": [{"path": "runtime.py", "freshness": "abc"}],
                "freshness": {"runtime.py": "abc"},
                "summary": "Resume from the latest checkpoint",
                "runtime_identity": {"workspace_fingerprint": "old-fingerprint"},
            }
        },
    }
    agent.session_store.save(agent.session)

    resumed = Pico.from_session(
        model_client=FakeModelClient(["<final>Resumed.</final>"]),
        workspace=build_workspace(tmp_path),
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert resumed.ask("Continue the task") == "Resumed."

    prompt = resumed.model_client.prompts[0]
    assert "Task checkpoint:" in prompt
    assert "Current goal: Fix failing resume flow" in prompt
    assert "Current blocker: Need to re-anchor stale file facts" in prompt
    assert "Next step: Re-read runtime.py and refresh the checkpoint" in prompt


def test_resume_invalidates_stale_file_summaries_and_marks_partial_stale(tmp_path):
    file_path = tmp_path / "runtime.py"
    file_path.write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, ["<final>checkpoint ready.</final>"])
    agent.memory.set_file_summary("runtime.py", "runtime.py: alpha")
    freshness = agent.memory.to_dict()["file_summaries"]["runtime.py"]["freshness"]
    agent.session["checkpoints"] = {
        "current_id": "ckpt_stale",
        "items": {
            "ckpt_stale": {
                "checkpoint_id": "ckpt_stale",
                "parent_checkpoint_id": "",
                "schema_version": "phase1-v1",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Fix stale summary handling",
                "completed": [],
                "excluded": [],
                "current_blocker": "",
                "next_step": "Re-read runtime.py",
                "key_files": [{"path": "runtime.py", "freshness": freshness}],
                "freshness": {"runtime.py": freshness},
                "summary": "runtime.py is important",
                "runtime_identity": {"workspace_fingerprint": agent.workspace.fingerprint()},
            }
        },
    }
    agent.session_store.save(agent.session)
    file_path.write_text("beta\n", encoding="utf-8")

    resumed = Pico.from_session(
        model_client=FakeModelClient(["<final>Resumed.</final>"]),
        workspace=build_workspace(tmp_path),
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert resumed.ask("Continue the task") == "Resumed."

    assert "runtime.py" not in resumed.memory.to_dict()["file_summaries"]
    assert resumed.last_prompt_metadata["resume_status"] == "partial-stale"
    assert resumed.last_prompt_metadata["stale_summary_invalidations"] == 1


def test_run_shell_nonzero_with_workspace_change_is_recorded_as_partial_success(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool(
        "run_shell",
        {
            "command": "printf 'changed\\n' > README.md && exit 1",
            "timeout": 20,
        },
    )

    assert "exit_code: 1" in result
    assert agent._last_tool_result_metadata["tool_status"] == "partial_success"
    assert agent._last_tool_result_metadata["affected_paths"] == ["README.md"]
    assert agent._last_tool_result_metadata["workspace_changed"] is True


def test_resume_marks_workspace_mismatch_when_checkpoint_runtime_identity_is_stale(tmp_path):
    agent = build_agent(tmp_path, ["<final>checkpoint ready.</final>"])
    agent.session["checkpoints"] = {
        "current_id": "ckpt_workspace",
        "items": {
            "ckpt_workspace": {
                "checkpoint_id": "ckpt_workspace",
                "parent_checkpoint_id": "",
                "schema_version": "phase1-v1",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Continue after drift",
                "completed": [],
                "excluded": [],
                "current_blocker": "",
                "next_step": "Rebuild runtime state",
                "key_files": [],
                "freshness": {},
                "summary": "workspace changed",
                "runtime_identity": {"workspace_fingerprint": "outdated-fingerprint"},
            }
        },
    }
    agent.session_store.save(agent.session)

    resumed = Pico.from_session(
        model_client=FakeModelClient(["<final>Resumed.</final>"]),
        workspace=build_workspace(tmp_path),
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert resumed.ask("Continue the task") == "Resumed."
    assert resumed.last_prompt_metadata["resume_status"] == "workspace-mismatch"


def test_write_file_trace_records_minimum_tool_contract_fields(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"write_file","args":{"path":"notes.txt","content":"hello\\n"}}</tool>',
            "<final>Done.</final>",
        ],
    )

    assert agent.ask("Create notes.txt") == "Done."

    trace_events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
    ]
    tool_event = [event for event in trace_events if event["event"] == "tool_executed"][-1]

    assert tool_event["name"] == "write_file"
    assert tool_event["risk_level"] == "high"
    assert tool_event["read_only"] is False
    assert tool_event["tool_status"] == "ok"
    assert tool_event["affected_paths"] == ["notes.txt"]
    assert tool_event["workspace_changed"] is True
    assert tool_event["diff_summary"] == ["created:notes.txt"]


def test_resume_marks_schema_mismatch_when_checkpoint_version_is_incompatible(tmp_path):
    agent = build_agent(tmp_path, ["<final>checkpoint ready.</final>"])
    agent.session["checkpoints"] = {
        "current_id": "ckpt_schema",
        "items": {
            "ckpt_schema": {
                "checkpoint_id": "ckpt_schema",
                "parent_checkpoint_id": "",
                "schema_version": "legacy-v0",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Continue after schema change",
                "completed": [],
                "excluded": [],
                "current_blocker": "",
                "next_step": "Migrate checkpoint",
                "key_files": [],
                "freshness": {},
                "summary": "schema changed",
                "runtime_identity": {"workspace_fingerprint": agent.workspace.fingerprint()},
            }
        },
    }
    agent.session_store.save(agent.session)

    resumed = Pico.from_session(
        model_client=FakeModelClient(["<final>Resumed.</final>"]),
        workspace=build_workspace(tmp_path),
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert resumed.ask("Continue the task") == "Resumed."
    assert resumed.last_prompt_metadata["resume_status"] == "schema-mismatch"


def test_resume_marks_no_checkpoint_when_session_has_no_checkpoint_state(tmp_path):
    agent = build_agent(tmp_path, ["<final>checkpoint ready.</final>"])
    agent.session.pop("checkpoints", None)
    agent.session_store.save(agent.session)

    resumed = Pico.from_session(
        model_client=FakeModelClient(["<final>Resumed.</final>"]),
        workspace=build_workspace(tmp_path),
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert resumed.ask("Continue the task") == "Resumed."
    assert resumed.last_prompt_metadata["resume_status"] == "no-checkpoint"
    assert "Task checkpoint:" not in resumed.model_client.prompts[0]


def test_freshness_mismatch_creates_checkpoint_before_model_completion(tmp_path):
    file_path = tmp_path / "runtime.py"
    file_path.write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, ["<final>Resumed.</final>"])
    agent.memory.set_file_summary("runtime.py", "runtime.py: alpha")
    freshness = agent.memory.to_dict()["file_summaries"]["runtime.py"]["freshness"]
    agent.session["checkpoints"] = {
        "current_id": "ckpt_freshness",
        "items": {
            "ckpt_freshness": {
                "checkpoint_id": "ckpt_freshness",
                "parent_checkpoint_id": "",
                "schema_version": "phase1-v1",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Handle freshness mismatch",
                "completed": [],
                "excluded": [],
                "current_blocker": "",
                "next_step": "Re-read runtime.py",
                "key_files": [{"path": "runtime.py", "freshness": freshness}],
                "freshness": {"runtime.py": freshness},
                "summary": "runtime.py changed",
                "runtime_identity": {"workspace_fingerprint": agent.workspace.fingerprint()},
            }
        },
    }
    agent.session_store.save(agent.session)
    file_path.write_text("beta\n", encoding="utf-8")

    assert agent.ask("Continue the task") == "Resumed."

    trace_events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
    ]
    checkpoint_events = [event for event in trace_events if event["event"] == "checkpoint_created"]

    assert checkpoint_events
    assert checkpoint_events[0]["trigger"] == "freshness_mismatch"


def test_runtime_identity_persists_key_execution_metadata(tmp_path):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    agent = Pico(
        model_client=FakeModelClient(["<final>Done.</final>"]),
        workspace=workspace,
        session_store=store,
        approval_policy="never",
        max_steps=9,
        max_new_tokens=1024,
        feature_flags={"memory": True, "relevant_memory": False},
    )

    runtime_identity = agent.session["runtime_identity"]

    assert runtime_identity["session_id"] == agent.session["id"]
    assert runtime_identity["cwd"] == str(tmp_path)
    assert runtime_identity["approval_policy"] == "never"
    assert runtime_identity["read_only"] is False
    assert runtime_identity["max_steps"] == 9
    assert runtime_identity["max_new_tokens"] == 1024
    assert runtime_identity["feature_flags"]["memory"] is True
    assert runtime_identity["feature_flags"]["relevant_memory"] is False
    assert runtime_identity["shell_env_allowlist"] == list(agent.shell_env_allowlist)


def test_resume_records_runtime_identity_mismatch_fields_in_metadata_and_trace(tmp_path):
    agent = build_agent(tmp_path, ["<final>checkpoint ready.</final>"])
    agent.session["checkpoints"] = {
        "current_id": "ckpt_identity",
        "items": {
            "ckpt_identity": {
                "checkpoint_id": "ckpt_identity",
                "parent_checkpoint_id": "",
                "schema_version": "phase1-v1",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Resume with a different runtime identity",
                "completed": [],
                "excluded": [],
                "current_blocker": "",
                "next_step": "Rebuild runtime identity",
                "key_files": [],
                "freshness": {},
                "summary": "identity changed",
                "runtime_identity": {
                    "workspace_fingerprint": agent.workspace.fingerprint(),
                    "approval_policy": "auto",
                    "read_only": False,
                    "max_steps": 6,
                    "max_new_tokens": 512,
                    "model": "old-model",
                    "model_client": "FakeModelClient",
                    "feature_flags": {"memory": True, "relevant_memory": True},
                    "shell_env_allowlist": ["PATH"],
                    "session_id": agent.session["id"],
                    "cwd": str(tmp_path),
                },
            }
        },
    }
    agent.session_store.save(agent.session)

    resumed = Pico.from_session(
        model_client=FakeModelClient(["<final>Resumed.</final>"]),
        workspace=build_workspace(tmp_path),
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="never",
        max_steps=9,
        max_new_tokens=1024,
        feature_flags={"memory": True, "relevant_memory": False},
    )

    resumed.ask("Continue the task")

    assert resumed.last_prompt_metadata["resume_status"] == "workspace-mismatch"
    assert resumed.last_prompt_metadata["runtime_identity_mismatch_fields"] == [
        "approval_policy",
        "feature_flags",
        "max_new_tokens",
        "max_steps",
        "model",
        "shell_env_allowlist",
    ]

    trace_events = [
        json.loads(line)
        for line in resumed.run_store.trace_path(resumed.current_task_state).read_text(encoding="utf-8").splitlines()
    ]
    mismatch_events = [event for event in trace_events if event["event"] == "runtime_identity_mismatch"]
    assert mismatch_events
    assert mismatch_events[0]["fields"] == [
        "approval_policy",
        "feature_flags",
        "max_new_tokens",
        "max_steps",
        "model",
        "shell_env_allowlist",
    ]
