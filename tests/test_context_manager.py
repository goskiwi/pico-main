import json

from pico.context_manager import ContextManager, _estimate_tokens
from tests.helpers import build_agent


def test_context_manager_assembles_sections_in_expected_order(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.memory.append_note("deploy key is red", tags=("deploy",), created_at="2026-04-07T10:00:00+00:00")
    agent.record({"role": "user", "content": "old request", "created_at": "2026-04-07T09:59:00+00:00"})
    agent.record({"role": "assistant", "content": "old answer", "created_at": "2026-04-07T10:00:30+00:00"})

    prompt, metadata = ContextManager(agent).build("Where is the deploy key?")

    assert prompt.index("You are pico") < prompt.index("Working")
    assert prompt.index("Working") < prompt.index("Relevant memory:")
    assert prompt.index("Relevant memory:") < prompt.index("Transcript:")
    assert prompt.index("Transcript:") < prompt.index("Current user request:")
    assert prompt.rstrip().endswith("Current user request:\nWhere is the deploy key?")
    assert metadata["section_order"] == ["prefix", "memory", "skills", "relevant_memory", "history", "current_request"]


def test_context_manager_injects_recent_runs_only_for_resume_requests(tmp_path):
    agent = build_agent(tmp_path, [])
    run_index = [
        {
            "run_id": "run_20260407",
            "task_id": "task_20260407",
            "task_goal": "Fix the parser bug.",
            "status": "completed",
            "stop_reason": "final_answer_returned",
            "updated_at": "2026-04-07T10:00:00+00:00",
            "task_graph_path": str(tmp_path / ".pico" / "runs" / "run_20260407" / "task_graph.mmd"),
            "report_path": str(tmp_path / ".pico" / "runs" / "run_20260407" / "report.json"),
        },
        {
            "run_id": "run_delegate",
            "task_id": "task_delegate",
            "task_goal": "Internal delegate investigation.",
            "status": "completed",
            "stop_reason": "final_answer_returned",
            "agent_mode": "explore",
            "parent_agent_id": "agent_parent",
            "updated_at": "2026-04-07T11:00:00+00:00",
            "task_graph_path": str(tmp_path / ".pico" / "runs" / "run_delegate" / "task_graph.mmd"),
            "report_path": str(tmp_path / ".pico" / "runs" / "run_delegate" / "report.json"),
        },
    ]
    (agent.run_store.root / "index.json").write_text(json.dumps(run_index), encoding="utf-8")

    ordinary_prompt, ordinary_metadata = ContextManager(agent).build("inspect README.md")
    resume_prompt, resume_metadata = ContextManager(agent).build("继续刚才那个 bug")

    assert "Recent runs:" not in ordinary_prompt
    assert ordinary_metadata["recent_runs"]["included"] is False
    assert "Recent runs:" in resume_prompt
    assert "Use read_file on task_graph, then read_tool_output for node refs." in resume_prompt
    assert "Fix the parser bug." in resume_prompt
    assert "Internal delegate investigation." not in resume_prompt
    assert "task_graph.mmd" in resume_prompt
    assert resume_metadata["recent_runs"]["included"] is True
    assert resume_metadata["recent_runs"]["selected_count"] == 1


def test_context_manager_reduces_relevant_memory_before_history_and_preserves_newer_context(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.prefix = "PREFIX " + ("A" * 600)
    agent.memory.render_memory_text = lambda: "MEMORY " + ("B" * 600)
    agent.memory.append_note("keep episodic note one " + ("C" * 220), tags=("keep",), created_at="2026-04-07T10:00:00+00:00")
    agent.memory.append_note("keep episodic note two " + ("D" * 220), tags=("keep",), created_at="2026-04-07T10:01:00+00:00")
    agent.memory.append_note("keep episodic note three " + ("E" * 220), tags=("keep",), created_at="2026-04-07T10:02:00+00:00")
    agent.record({"role": "user", "content": "OLD-CONTEXT " + ("D" * 260), "created_at": "2026-04-07T09:59:00+00:00"})
    for minute in range(1, 8):
        role = "assistant" if minute % 2 == 1 else "user"
        content = "RECENT-CONTEXT " + ("E" * 260) if minute == 7 else f"recent-{minute} " + ("E" * 180)
        agent.record({"role": role, "content": content, "created_at": f"2026-04-07T10:0{minute}:00+00:00"})

    manager = ContextManager(
        agent,
        total_budget=700,
        section_budgets={
            "prefix": 120,
            "memory": 120,
            "relevant_memory": 120,
            "history": 700,
        },
    )

    prompt, metadata = manager.build("keep this request verbatim")

    for section in ("prefix", "memory", "relevant_memory", "history"):
        assert metadata["sections"][section]["rendered_estimated_tokens"] <= metadata["sections"][section]["budget_tokens"]

    reduction_sections = [entry["section"] for entry in metadata["budget_reductions"]]
    assert reduction_sections[0] == "relevant_memory"
    assert reduction_sections
    assert "RECENT-CONTEXT" in prompt
    assert "Task graph:" in prompt
    assert "keep this request verbatim" in prompt


def test_context_manager_renders_top_three_episodic_notes_per_note_under_budget(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.memory.append_note("alpha episodic note " + ("A" * 120), tags=("recall",), created_at="2026-04-07T10:00:00+00:00")
    agent.memory.append_note("beta episodic recall note " + ("B" * 120), created_at="2026-04-07T10:01:00+00:00")
    agent.memory.append_note("gamma episodic note " + ("C" * 120), tags=("recall",), created_at="2026-04-07T10:02:00+00:00")
    agent.memory.append_note("older unmatched note", created_at="2026-04-07T09:59:00+00:00")
    agent.memory.append_note("Unrelated note", created_at="2026-04-07T11:00:00+00:00")

    prompt, metadata = ContextManager(
        agent,
        total_budget=250,
        section_budgets={
            "prefix": 60,
            "memory": 60,
            "relevant_memory": 80,
            "history": 60,
        },
    ).build("recall")

    assert metadata["relevant_memory"]["selected_count"] == 3
    assert metadata["relevant_memory"]["limit"] == 3
    assert metadata["relevant_memory"]["selected_notes"] == [
        "gamma episodic note " + ("C" * 120),
        "alpha episodic note " + ("A" * 120),
        "beta episodic recall note " + ("B" * 120),
    ]
    assert len(metadata["relevant_memory"]["rendered_notes"]) == 3
    assert metadata["relevant_memory"]["rendered_count"] == 3
    assert metadata["relevant_memory"]["rendered_notes"][0].startswith("gamma episodi")
    assert metadata["relevant_memory"]["rendered_notes"][1].startswith("alpha episodi")
    assert metadata["relevant_memory"]["rendered_notes"][2].startswith("beta episodi")
    relevant_section = prompt.split("Relevant memory:\n", 1)[1].split("\n\nTranscript:", 1)[0]
    assert len([line for line in relevant_section.splitlines() if line.startswith("- ")]) == 3
    assert "alpha episodi" in relevant_section
    assert "beta episodic" in relevant_section
    assert "gamma episodi" in relevant_section
    assert "older unmatched note" not in relevant_section


def test_context_manager_preserves_current_request_when_over_budget(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.prefix = "PREFIX " + ("A" * 600)
    agent.memory.render_memory_text = lambda: "MEMORY " + ("B" * 600)
    agent.memory.retrieval_view = lambda query, limit=3: "Relevant memory:\n" + "\n".join(f"- {i} " + ("C" * 220) for i in range(5))
    agent.history_text = lambda: "Transcript:\n" + "\n".join(f"[user] {i} " + ("D" * 220) for i in range(5))

    request = "please preserve this request exactly"
    prompt, metadata = ContextManager(
        agent,
        total_budget=250,
        section_budgets={
            "prefix": 80,
            "memory": 80,
            "relevant_memory": 80,
            "history": 80,
        },
    ).build(request)

    assert prompt.split("Current user request:\n", 1)[1] == request
    assert metadata["current_request"]["text"] == request
    assert metadata["current_request"]["rendered_chars"] == len(request)


def test_context_manager_clips_huge_request_to_hard_budget(tmp_path):
    agent = build_agent(tmp_path, [])
    request = "BEGIN " + ("A" * 20_000) + " END"

    prompt, metadata = ContextManager(agent, total_budget=1000).build(request)

    assert _estimate_tokens(prompt) <= 1000
    assert metadata["prompt_over_budget"] is False
    assert metadata["current_request"]["truncated"] is True
    assert "BEGIN" in prompt
    assert "END" in prompt
    assert "[request truncated]" in prompt


def test_context_manager_records_estimated_token_budget_metadata(tmp_path):
    agent = build_agent(tmp_path, [])

    prompt, metadata = ContextManager(agent).build("请检查 README.md and summarize it")

    assert metadata["prompt_estimated_tokens"] > 0
    assert metadata["prompt_budget_tokens"] > 0
    assert metadata["section_budgets_tokens"]["prefix"] > 0
    assert metadata["section_budgets_tokens"]["current_request"] is None
    assert metadata["sections"]["prefix"]["rendered_estimated_tokens"] > 0
    assert metadata["sections"]["current_request"]["budget_tokens"] is None
    assert metadata["current_request"]["estimated_tokens"] > 0
    assert metadata["prompt_estimated_tokens"] <= len(prompt)
    assert metadata["prompt_estimated_tokens"] == _estimate_tokens(prompt)


def test_context_manager_collapses_older_duplicate_reads_into_one_summary_line(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\nbeta\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])
    agent.memory.set_file_summary("sample.txt", "alpha | beta")
    agent.memory.remember_file("sample.txt")

    for created_at in ("2026-04-07T09:00:00+00:00", "2026-04-07T09:01:00+00:00"):
        agent.record(
            {
                "role": "tool",
                "name": "read_file",
                "args": {"path": "sample.txt", "start": 1, "end": 2},
                "content": "# sample.txt\nalpha\nbeta\n",
                "created_at": created_at,
            }
        )

    for minute in range(2, 8):
        role = "user" if minute % 2 == 0 else "assistant"
        agent.record(
            {
                "role": role,
                "content": f"recent-{minute}",
                "created_at": f"2026-04-07T09:0{minute}:00+00:00",
            }
        )

    prompt, metadata = ContextManager(agent).build("check the file")
    transcript = prompt.split("\n\nTranscript:\n", 1)[1].split("\n\nCurrent user request:", 1)[0]

    assert transcript.count("[tool:read_file]") == 0
    assert "sample.txt -> alpha | beta" in transcript
    assert metadata["history"]["older_entries_count"] == 2
    assert metadata["history"]["collapsed_duplicate_reads"] == 1
    assert metadata["history"]["reused_file_summary_count"] == 1


def test_context_manager_summarizes_older_tool_output_into_one_line(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.record(
        {
            "role": "tool",
            "name": "run_shell",
            "args": {"command": "pytest -q"},
            "content": "FAIL test_one\nFAIL test_two\nFAIL test_three\nFAIL test_four\n",
            "created_at": "2026-04-07T09:00:00+00:00",
        }
    )

    for minute in range(1, 7):
        role = "user" if minute % 2 == 1 else "assistant"
        agent.record(
            {
                "role": role,
                "content": f"recent-{minute}",
                "created_at": f"2026-04-07T09:0{minute}:00+00:00",
            }
        )

    prompt, metadata = ContextManager(agent).build("check failures")
    transcript = prompt.split("\n\nTranscript:\n", 1)[1].split("\n\nCurrent user request:", 1)[0]

    assert 'pytest -q -> FAIL test_one | FAIL test_two | FAIL test_three' in transcript
    assert "FAIL test_four" not in transcript
    assert metadata["history"]["summarized_tool_count"] == 1
    assert metadata["history"]["reused_file_summary_count"] == 0


def test_context_manager_shell_summary_prioritizes_failure_lines(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.record(
        {
            "role": "tool",
            "name": "run_shell",
            "args": {"command": "pytest -q"},
            "content": "collecting tests\nsetup ok\nTraceback most recent call\nAssertionError: bad value\nshort summary\n",
            "created_at": "2026-04-07T09:00:00+00:00",
        }
    )

    for minute in range(1, 7):
        role = "user" if minute % 2 == 1 else "assistant"
        agent.record(
            {
                "role": role,
                "content": f"recent-{minute}",
                "created_at": f"2026-04-07T09:0{minute}:00+00:00",
            }
        )

    prompt, metadata = ContextManager(agent).build("check failures")
    transcript = prompt.split("\n\nTranscript:\n", 1)[1].split("\n\nCurrent user request:", 1)[0]

    assert "pytest -q -> Traceback most recent call | AssertionError: bad value" in transcript
    assert "collecting tests" not in transcript
    assert metadata["history"]["summarized_tool_count"] == 1


def test_context_manager_uses_llm_history_compaction_when_enabled(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "flowchart TD\n"
            "  G[\"goal | open | Keep the deploy fix moving.\"]\n"
            "  N1[\"next | open | Inspect auth.py.\"]\n"
            "  G --> N1\n"
        ],
        feature_flags={"llm_history_compaction": True},
    )
    for index in range(8):
        agent.record(
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"older-context-{index} " + ("A" * 120),
                "created_at": f"2026-04-07T09:0{index}:00+00:00",
            }
        )
    agent.record(
        {
            "role": "assistant",
            "content": "recent decision: keep retry budget low",
            "created_at": "2026-04-07T09:20:00+00:00",
        }
    )

    prompt, metadata = ContextManager(
        agent,
        section_budgets={
            "prefix": 300,
            "memory": 300,
            "relevant_memory": 120,
            "history": 200,
        },
    ).build("continue")

    assert "You are compacting a coding agent transcript." in agent.model_client.prompts[0]
    assert "flowchart TD" in agent.model_client.prompts[0]
    assert "Task graph:" in prompt
    assert "Session compact summary:" not in prompt
    assert "Keep the deploy fix moving." in prompt
    assert "Recent transcript:" in prompt
    assert "recent decision: keep retry budget low" in prompt
    assert metadata["history"]["llm_compact_used"] is True


def test_context_manager_sanitizes_compacted_history_to_mermaid_flowchart(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>flowchart TD\n"
            "  A[\"goal | open | Continue <b>work</b>`\"]\n"
            "  B[\"tool | done | ref: .pico/runs/run_1/tool_outputs/0001_read_file.txt\"]\n"
            "  A --> B\n"
            "</final>"
        ],
        feature_flags={"llm_history_compaction": True},
    )
    for index in range(8):
        agent.record({"role": "user", "content": f"older {index} " + ("A" * 120), "created_at": "2026-04-07T09:00:00+00:00"})

    prompt, _ = ContextManager(agent, section_budgets={"history": 260}).build("continue")

    graph = prompt.split("Task graph:\n", 1)[1].split("\nRecent transcript:", 1)[0]
    assert "flowchart TD" in graph
    assert "<final>" not in graph
    assert "<b>" not in graph
    assert "`" not in graph


def test_context_manager_relevant_memory_can_mix_durable_notes(tmp_path):
    memory_root = tmp_path / ".pico" / "memory"
    entries_dir = memory_root / "entries"
    entries_dir.mkdir(parents=True)
    (memory_root / "MEMORY.md").write_text(
        "# Durable Memory Index\n\n"
        "- [project](entries/project.md): Project Memory\n"
        "  - summary: Project decisions, constraints, and dynamics.\n"
        "  - tags: project\n",
        encoding="utf-8",
    )
    (entries_dir / "project.md").write_text(
        "# Project Memory\n\n"
        "- type: project\n"
        "- summary: Project decisions, constraints, and dynamics.\n"
        "- tags: project\n"
        "- updated_at: 2026-04-12T08:14:49+00:00\n\n"
        "## Notes\n"
        "- Use constrained tools instead of guessing.\n",
        encoding="utf-8",
    )

    agent = build_agent(tmp_path, [])

    prompt, metadata = ContextManager(agent).build("Should I use constrained tools?")
    relevant_section = prompt.split("Relevant memory:\n", 1)[1].split("\n\nTranscript:", 1)[0]

    assert "Use constrained tools instead of guessing." in relevant_section
    assert "verify before acting" in relevant_section
    assert any("Use constrained tools instead of guessing." in item for item in metadata["relevant_memory"]["selected_notes"])
    assert metadata["relevant_memory"]["selected_durable_count"] == 1
    assert metadata["relevant_memory"]["selected_sources"] == ["project"]
    assert metadata["relevant_memory"]["selected_kinds"] == ["durable"]
