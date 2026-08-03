import tiktoken

from pico.context_manager import ContextManager
from pico.context_types import _token_clip, count_tokens, tokenizer_details
from tests.helpers import build_agent


def test_token_budget_uses_tiktoken_and_clips_to_the_real_token_limit():
    text = "中文 mixed Python: def greet(name): return f'hello {name}'" * 4
    encoding = tiktoken.get_encoding("o200k_base")

    assert count_tokens(text) == len(encoding.encode(text, disallowed_special=()))

    clipped = _token_clip(text, 20)

    assert count_tokens(clipped) <= 20
    assert tokenizer_details("gpt-4o")["model_mapping_known"] is True


def test_context_budget_reduces_old_sections_before_truncating_current_request(tmp_path):
    agent = build_agent(
        tmp_path,
        [],
        feature_flags={"repo_map": False, "dynamic_budget": False},
    )
    agent.prefix = "PREFIX_HISTORY " + ("old baseline detail " * 300)
    for index in range(4):
        agent.memory.append_note(
            f"MEMORY_HISTORY_{index} " + ("previous investigation detail " * 100),
            source="test",
        )

    manager = ContextManager(
        agent,
        total_budget=220,
        section_budgets={
            "prefix": 180,
            "memory": 180,
            "skills": 0,
            "repo_map": 0,
            "task_context": 80,
        },
        section_floors={
            "prefix": 0,
            "memory": 0,
            "skills": 0,
            "repo_map": 0,
            "task_context": 0,
        },
    )
    request = (
        "CURRENT_REQUEST_BEGIN update target.py with the verified fix "
        "and run its focused test CURRENT_REQUEST_END"
    )

    prompt, metadata = manager.build(request)

    assert agent.count_tokens(prompt) <= 220
    assert f"Current user request:\n{request}" in prompt
    assert metadata["current_request"]["truncated"] is False
    assert metadata["budget_reductions"]
    assert {
        entry["section"] for entry in metadata["budget_reductions"]
    } <= {"prefix", "memory", "skills", "repo_map", "task_context"}
    assert metadata["sections"]["prefix"]["rendered_tokens"] < metadata["sections"]["prefix"]["raw_tokens"]
