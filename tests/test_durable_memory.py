import json

from tests.helpers import build_agent


def test_explicit_memory_promotion_persists_durable_memory_entries(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>Project: Use constrained tools instead of guessing.\n"
            "Feedback: Prefer local agent state under .pico/.\n"
            "Reference: Benchmark artifacts live under artifacts/.</final>",
        ],
    )

    answer = agent.ask(
        "Capture the stable facts you already discovered as durable memory. "
        "Respond with exactly the long-term facts."
    )

    assert "Project:" in answer

    index_path = tmp_path / ".pico" / "memory" / "MEMORY.md"
    project_path = tmp_path / ".pico" / "memory" / "entries" / "project.md"
    feedback_path = tmp_path / ".pico" / "memory" / "entries" / "feedback.md"
    reference_path = tmp_path / ".pico" / "memory" / "entries" / "reference.md"
    report = json.loads(agent.run_store.report_path(agent.current_task_state).read_text(encoding="utf-8"))

    assert index_path.exists()
    assert project_path.exists()
    assert feedback_path.exists()
    assert reference_path.exists()
    assert "project" in index_path.read_text(encoding="utf-8")
    assert "Use constrained tools instead of guessing." in project_path.read_text(encoding="utf-8")
    assert "Prefer local agent state under .pico/." in feedback_path.read_text(encoding="utf-8")
    assert "Benchmark artifacts live under artifacts/." in reference_path.read_text(encoding="utf-8")
    assert report["durable_promotions"] == [
        "project: Use constrained tools instead of guessing.",
        "feedback: Prefer local agent state under .pico/.",
        "reference: Benchmark artifacts live under artifacts/.",
    ]


def test_explicit_memory_promotion_supports_chinese_intent_and_labels(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>项目：优先使用受约束工具，不要靠猜。\n"
            "反馈：持久记忆保持轻量、按 type 管理。</final>",
        ],
    )

    answer = agent.ask("请把下面这些稳定事实记住，作为长期记忆保存下来。")

    assert "项目：" in answer

    project_path = tmp_path / ".pico" / "memory" / "entries" / "project.md"
    feedback_path = tmp_path / ".pico" / "memory" / "entries" / "feedback.md"

    assert "优先使用受约束工具，不要靠猜。" in project_path.read_text(encoding="utf-8")
    assert "持久记忆保持轻量、按 type 管理。" in feedback_path.read_text(encoding="utf-8")


def test_explicit_memory_promotion_rejects_secret_shaped_and_transient_lines(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>Project: Use constrained tools instead of guessing.\n"
            "Reference: API key is sk-live-secret-abc.\n"
            "Project: Current goal is fix flaky tests.\n"
            "Reference: stdout: FAIL test_one FAIL test_two FAIL test_three.</final>",
        ],
    )

    agent.ask("Capture these stable facts into durable memory.")

    report = json.loads(agent.run_store.report_path(agent.current_task_state).read_text(encoding="utf-8"))
    project_path = tmp_path / ".pico" / "memory" / "entries" / "project.md"
    reference_path = tmp_path / ".pico" / "memory" / "entries" / "reference.md"

    assert report["durable_promotions"] == [
        "project: Use constrained tools instead of guessing.",
    ]
    assert report["durable_rejections"] == [
        "reference:secret_shaped",
        "project:transient_task_state",
        "reference:noisy_output",
    ]
    assert "Use constrained tools instead of guessing." in project_path.read_text(encoding="utf-8")
    assert not reference_path.exists()


def test_explicit_memory_promotion_supersedes_matching_durable_fact(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>Reference: Python runtime is 3.11.</final>",
            "<final>Reference: Python runtime is 3.12.</final>",
        ],
    )

    assert agent.ask("Capture this stable dependency fact into durable memory.") == "Reference: Python runtime is 3.11."
    assert agent.ask("Save the updated dependency fact into durable memory.") == "Reference: Python runtime is 3.12."

    reference_path = tmp_path / ".pico" / "memory" / "entries" / "reference.md"
    report = json.loads(agent.run_store.report_path(agent.current_task_state).read_text(encoding="utf-8"))
    text = reference_path.read_text(encoding="utf-8")

    assert "Python runtime is 3.12." in text
    assert "Python runtime is 3.11." not in text
    assert report["durable_superseded"] == [
        "reference: Python runtime is 3.11. -> Python runtime is 3.12.",
    ]


def test_explicit_memory_promotion_dedupes_duplicate_durable_note(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>Project: Use constrained tools instead of guessing.</final>",
            "<final>Project: Use constrained tools instead of guessing.</final>",
        ],
    )

    agent.ask("Capture the stable fact into durable memory.")
    agent.ask("Capture the stable fact into durable memory again.")

    project_path = tmp_path / ".pico" / "memory" / "entries" / "project.md"
    text = project_path.read_text(encoding="utf-8")

    assert text.count("- text: Use constrained tools instead of guessing.") == 1


def test_runtime_llm_promotes_long_term_signals_after_success(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>Done.</final>",
            json.dumps(
                {
                    "memories": [
                        {"type": "user", "text": "User mostly works with Go."},
                        {"type": "feedback", "text": "User prefers not adding dependencies without asking."},
                        {"type": "project", "text": "This repository keeps runtime dependencies at zero."},
                        {"type": "reference", "text": "Deployment details should be checked in the internal runbook."},
                    ]
                }
            ),
        ],
        feature_flags={"llm_memory_extract": True},
    )

    answer = agent.ask(
        "I mostly work with Go. Please do not add dependencies without asking. "
        "This repo keeps runtime dependencies at zero. Check the internal runbook for deploys."
    )

    assert answer == "Done."

    report = json.loads(agent.run_store.report_path(agent.current_task_state).read_text(encoding="utf-8"))
    memory_root = tmp_path / ".pico" / "memory" / "entries"

    assert report["durable_promotions"] == []
    assert report["llm_durable_promotions"] == [
        "user: User mostly works with Go.",
        "feedback: User prefers not adding dependencies without asking.",
        "project: This repository keeps runtime dependencies at zero.",
        "reference: Deployment details should be checked in the internal runbook.",
    ]
    assert "User mostly works with Go." in (memory_root / "user.md").read_text(encoding="utf-8")
    assert "User prefers not adding dependencies without asking." in (memory_root / "feedback.md").read_text(encoding="utf-8")
    assert "This repository keeps runtime dependencies at zero." in (memory_root / "project.md").read_text(encoding="utf-8")
    assert "Deployment details should be checked in the internal runbook." in (memory_root / "reference.md").read_text(encoding="utf-8")
    assert "durable memory extractor" in agent.model_client.prompts[1]


def test_runtime_llm_promotion_can_be_disabled(tmp_path):
    agent = build_agent(
        tmp_path,
        ["<final>Done.</final>"],
        feature_flags={"llm_memory_extract": False},
    )

    agent.ask("I mostly work with Go.")

    report = json.loads(agent.run_store.report_path(agent.current_task_state).read_text(encoding="utf-8"))

    assert report["llm_durable_promotions"] == []
    assert not (tmp_path / ".pico" / "memory" / "entries" / "user.md").exists()


def test_durable_memory_promotion_flag_disables_rule_and_llm_paths(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>Project: Delegate findings stay ephemeral.</final>",
            '{"memories":[{"type":"project","text":"must not run"}]}',
        ],
        feature_flags={
            "durable_memory_promotion": False,
            "llm_memory_extract": True,
        },
    )

    answer = agent.ask("Remember this stable project fact as durable memory.")

    report = json.loads(
        agent.run_store.report_path(agent.current_task_state).read_text(encoding="utf-8")
    )
    assert answer == "Project: Delegate findings stay ephemeral."
    assert len(agent.model_client.prompts) == 1
    assert report["durable_promotions"] == []
    assert report["llm_durable_promotions"] == []
    assert not (tmp_path / ".pico" / "memory").exists()


def test_runtime_llm_promotion_skips_unsuccessful_runs(tmp_path):
    agent = build_agent(tmp_path, ['<tool>{"name":"list_files","args":{"path":"."}}</tool>'], max_steps=1)

    answer = agent.ask("I mostly work with Go.")

    report = json.loads(agent.run_store.report_path(agent.current_task_state).read_text(encoding="utf-8"))

    assert answer == "Stopped after reaching the step limit without a final answer."
    assert report["llm_durable_promotions"] == []
    assert not (tmp_path / ".pico" / "memory" / "entries" / "user.md").exists()
