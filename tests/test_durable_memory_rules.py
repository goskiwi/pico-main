import pico.durable_memory as durable_memory


def test_extract_promotions_requires_explicit_memory_intent():
    answer = "Project: Use constrained tools."

    assert durable_memory.extract_promotions("Summarize this.", answer) == ([], [])
    assert durable_memory.extract_promotions("Remember this.", answer) == (
        [("project", "Use constrained tools.")],
        [],
    )


def test_extract_promotions_rejects_secret_transient_and_noisy_facts():
    answer = "\n".join(
        [
            "Project: Use constrained tools.",
            "Reference: API key is sk-live-secret-abc.",
            "Project: Current goal is fix flaky tests.",
            "Reference: stdout: FAIL test_one FAIL test_two FAIL test_three.",
            "Feedback: README.md:12 says use foo.",
        ]
    )

    promotions, rejections = durable_memory.extract_promotions("Capture these facts.", answer)

    assert promotions == [("project", "Use constrained tools.")]
    assert rejections == [
        "reference:secret_shaped",
        "project:transient_task_state",
        "reference:noisy_output",
        "feedback:code_location",
    ]


def test_extract_promotions_supports_chinese_labels_and_intent():
    promotions, rejections = durable_memory.extract_promotions(
        "请记住这些稳定事实。",
        "项目：优先使用受约束工具。\n反馈：持久记忆保持轻量。",
    )

    assert promotions == [
        ("project", "优先使用受约束工具。"),
        ("feedback", "持久记忆保持轻量。"),
    ]
    assert rejections == []


def test_promote_candidates_rejects_unsafe_llm_candidates(tmp_path):
    from pico.memory import LayeredMemory

    memory = LayeredMemory(workspace_root=tmp_path)
    result = durable_memory.promote_candidates(
        memory,
        [
            ("feedback", "Do not use the API key sk-live-secret."),
            ("project", "README.md:12 stores the helper."),
            ("project", "Current goal is fix tests."),
            ("nonsense", "This type is not supported."),
            ("feedback", "Prefer concise final answers."),
        ],
    )

    assert result.promoted == ["feedback: Prefer concise final answers."]
    assert result.rejections == [
        "feedback:secret_shaped",
        "project:code_location",
        "project:transient_task_state",
        "nonsense:invalid_type",
    ]
