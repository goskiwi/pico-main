from url_tools import append_query


def test_appends_to_plain_url():
    assert append_query("https://example.test/items", {"page": 2}) == (
        "https://example.test/items?page=2"
    )


def test_empty_params_leave_url_unchanged():
    url = "https://example.test/items"
    assert append_query(url, {}) == url
