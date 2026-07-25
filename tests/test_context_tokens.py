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


def test_prompt_metadata_reports_real_tokenizer_and_token_counts(tmp_path):
    agent = build_agent(tmp_path, [])

    prompt, metadata = ContextManager(agent, total_budget=500).build("检查 README.md")

    assert metadata["prompt_tokens"] == agent.count_tokens(prompt)
    assert metadata["tokenizer"]["encoding"] == "o200k_base"
    assert metadata["sections"]["prefix"]["rendered_tokens"] <= metadata["sections"]["prefix"]["budget_tokens"]
