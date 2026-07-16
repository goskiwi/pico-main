from config_loader import load_config


def test_environment_overrides_file_and_default_values():
    result = load_config(
        {"port": "7000"},
        {"port": "8000"},
        {"APP_PORT": "9000"},
    )
    assert result["port"] == "9000"
