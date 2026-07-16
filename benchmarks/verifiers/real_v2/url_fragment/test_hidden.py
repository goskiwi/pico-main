from url_tools import append_query


def test_query_is_inserted_before_fragment():
    assert append_query("https://example.test/items#top", {"q": "red blue"}) == (
        "https://example.test/items?q=red+blue#top"
    )


def test_existing_query_and_fragment_are_both_preserved():
    assert append_query("https://example.test/items?a=1#top", {"b": "x/y"}) == (
        "https://example.test/items?a=1&b=x%2Fy#top"
    )


def test_empty_params_preserve_complex_url_byte_for_byte():
    url = "https://example.test/items?a=1#top"
    assert append_query(url, {}) == url
