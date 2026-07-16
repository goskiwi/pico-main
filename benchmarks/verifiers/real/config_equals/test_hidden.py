from config_loader import parse_config


def test_value_may_contain_additional_equals_characters():
    assert parse_config(["token=header.payload=signature"]) == {
        "token": "header.payload=signature"
    }


def test_key_and_value_are_still_trimmed():
    assert parse_config([" url = https://example.test/?a=b=c "]) == {
        "url": "https://example.test/?a=b=c"
    }
