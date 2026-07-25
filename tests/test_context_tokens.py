import tiktoken

from pico.context_types import _token_clip, count_tokens, tokenizer_details


def test_token_budget_uses_tiktoken_and_clips_to_the_real_token_limit():
    text = "中文 mixed Python: def greet(name): return f'hello {name}'" * 4
    encoding = tiktoken.get_encoding("o200k_base")

    assert count_tokens(text) == len(encoding.encode(text, disallowed_special=()))

    clipped = _token_clip(text, 20)

    assert count_tokens(clipped) <= 20
    assert tokenizer_details("gpt-4o")["model_mapping_known"] is True
