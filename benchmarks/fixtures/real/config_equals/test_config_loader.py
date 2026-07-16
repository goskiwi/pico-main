from config_loader import parse_config


def test_parse_config_reads_key_value_pairs():
    assert parse_config(["host = localhost", "port=8080"]) == {
        "host": "localhost",
        "port": "8080",
    }


def test_parse_config_ignores_comments_and_blanks():
    assert parse_config(["", "  # comment", "debug=true"]) == {"debug": "true"}
