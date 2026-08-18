import os

from pico.config import find_project_env, load_env_file, load_project_env, provider_env


def test_env_local_is_discovered_and_overrides_base_env(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("PICO_TEST_VALUE=base\nBASE_ONLY=yes\n")
    (tmp_path / ".env.local").write_text("PICO_TEST_VALUE=local\n")
    nested = tmp_path / "src" / "package"
    nested.mkdir(parents=True)
    monkeypatch.delenv("PICO_TEST_VALUE", raising=False)
    monkeypatch.delenv("BASE_ONLY", raising=False)

    assert find_project_env(nested) == tmp_path / ".env.local"
    loaded = load_project_env(nested)

    assert loaded == {"PICO_TEST_VALUE": "local", "BASE_ONLY": "yes"}
    assert os.environ["PICO_TEST_VALUE"] == "local"
    assert os.environ["BASE_ONLY"] == "yes"


def test_provider_env_does_not_read_legacy_names(monkeypatch):
    monkeypatch.delenv("PICO_OPENAI_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_MODEL", "legacy-model")

    assert provider_env("PICO_OPENAI_MODEL", "current-default") == "current-default"


def test_explicit_env_file_loads_only_the_selected_file(tmp_path, monkeypatch):
    selected = tmp_path / "trusted.env"
    selected.write_text("PICO_EXPLICIT_VALUE=trusted\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("PICO_EXPLICIT_VALUE=untrusted\n")
    monkeypatch.delenv("PICO_EXPLICIT_VALUE", raising=False)

    assert load_env_file(selected) == {"PICO_EXPLICIT_VALUE": "trusted"}
    assert os.environ["PICO_EXPLICIT_VALUE"] == "trusted"
