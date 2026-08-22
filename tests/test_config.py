import os

import pytest

from pico.config import find_project_env, load_project_env, provider_env


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


def test_project_env_does_not_override_existing_process_environment(
    tmp_path,
    monkeypatch,
):
    (tmp_path / ".env").write_text(
        "PICO_OPENAI_API_BASE=https://repo.example/v1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "PICO_OPENAI_API_BASE",
        "https://trusted.example/v1",
    )

    loaded = load_project_env(tmp_path, boundary=tmp_path)

    assert loaded["PICO_OPENAI_API_BASE"] == "https://repo.example/v1"
    assert os.environ["PICO_OPENAI_API_BASE"] == "https://trusted.example/v1"


def test_project_env_search_stops_at_workspace_boundary(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / ".env").write_text("PICO_PARENT_VALUE=outside\n", encoding="utf-8")
    monkeypatch.delenv("PICO_PARENT_VALUE", raising=False)

    assert load_project_env(repo, boundary=repo) == {}
    assert "PICO_PARENT_VALUE" not in os.environ


def test_project_env_rejects_symlink_and_redacts_invalid_line(tmp_path):
    outside = tmp_path / "outside.env"
    outside.write_text("PICO_VALUE=outside\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").symlink_to(outside)

    with pytest.raises(ValueError, match="must not be a symlink"):
        load_project_env(repo, boundary=repo)

    (repo / ".env").unlink()
    secret = "do-not-echo-this-secret"
    (repo / ".env").write_text(f"PICO_KEY {secret}\n", encoding="utf-8")

    with pytest.raises(ValueError) as raised:
        load_project_env(repo, boundary=repo)

    assert ".env:1" in str(raised.value)
    assert secret not in str(raised.value)
