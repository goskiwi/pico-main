"""Direct contracts for tokenizer budgeting and Python repository understanding."""

from pathlib import Path

import tiktoken

from pico.repo_map import RepoMap, _token_clip, count_tokens


def _write_python_repo(root):
    (root / "app").mkdir()
    (root / "tests").mkdir()
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "models.py").write_text(
        "class User:\n    def save(self):\n        return 'saved'\n",
        encoding="utf-8",
    )
    (root / "app" / "services.py").write_text(
        "from app.models import User\n\n"
        "class UserService:\n"
        "    def create_user(self):\n"
        "        user = User()\n"
        "        user.save()\n"
        "        return user\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_services.py").write_text(
        "from app.dependencies import UserService\n\n"
        "def test_create_user_saves_once():\n"
        "    assert UserService().create_user()\n",
        encoding="utf-8",
    )


def test_repo_map_ranks_cross_file_implementation_and_test_relations(tmp_path):
    _write_python_repo(tmp_path)

    result = RepoMap(tmp_path).render(
        "Fix UserService.create_user duplicate save and update its test",
        budget_tokens=800,
        max_results=20,
    )

    assert "app/services.py" in result.text
    assert "UserService.create_user" in result.text
    assert "tests/test_services.py" in result.text
    assert "test_create_user_saves_once" in result.text
    assert result.details["parsed_files"] == 4
    assert result.details["graph_edges"] > 0


def test_repo_map_budget_excludes_generated_and_unrelated_components(tmp_path):
    _write_python_repo(tmp_path)
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "generated.py").write_text(
        "def generated_symbol():\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "unrelated.py").write_text(
        "class MetricsCache:\n    def get(self, key):\n        return key\n",
        encoding="utf-8",
    )

    result = RepoMap(tmp_path).render(
        "Fix UserService.create_user duplicate save",
        budget_tokens=180,
        max_results=60,
    )

    assert "UserService.create_user" in result.text
    assert "generated_symbol" not in result.text
    assert "MetricsCache" not in result.text
    assert result.details["skipped_files"] >= 1
    assert count_tokens(result.text) <= 180


def test_token_clipping_uses_the_real_tokenizer_limit():
    text = "中文 mixed Python: def greet(name): return f'hello {name}'" * 4
    encoding = tiktoken.get_encoding("o200k_base")

    assert count_tokens(text) == len(encoding.encode(text, disallowed_special=()))
    assert count_tokens(_token_clip(text, 20)) <= 20
    external_counter = lambda value: len(str(value)) * 2
    assert external_counter(
        _token_clip(text, 20, token_counter=external_counter)
    ) <= 20


def test_repo_map_cache_invalidates_only_changed_and_deleted_files(tmp_path):
    _write_python_repo(tmp_path)
    repo_map = RepoMap(tmp_path)

    first = repo_map.refresh()
    second = repo_map.refresh()

    assert first.cache_misses == 4
    assert second.cache_hits == 4
    assert second.cache_misses == 0

    services = tmp_path / "app" / "services.py"
    services.write_text(
        services.read_text(encoding="utf-8")
        + "\ndef delete_user():\n    return None\n",
        encoding="utf-8",
    )
    changed = repo_map.refresh()

    assert changed.cache_hits == 3
    assert changed.cache_misses == 1
    assert any(symbol.name == "delete_user" for symbol in changed.symbols.values())

    (tmp_path / "app" / "models.py").unlink()
    deleted = repo_map.refresh()

    assert deleted.parsed_files == 3
    assert all(symbol.path != "app/models.py" for symbol in deleted.symbols.values())


def test_repo_map_counts_parse_errors_without_failing_the_map(tmp_path):
    (tmp_path / "valid.py").write_text(
        "def valid_symbol():\n    return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "broken.py").write_text(
        "def broken(:\n    return 1\n",
        encoding="utf-8",
    )

    result = RepoMap(tmp_path).render("valid symbol", budget_tokens=200)

    assert "valid_symbol" in result.text
    assert result.details["parse_error_files"] == 1


def test_repo_map_skips_python_symlinks(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("def outside_symbol():\n    return 1\n", encoding="utf-8")
    (tmp_path / "inside.py").write_text(
        "def inside_symbol():\n    return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "linked.py").symlink_to(outside)

    snapshot = RepoMap(tmp_path).refresh()

    assert any(symbol.name == "inside_symbol" for symbol in snapshot.symbols.values())
    assert all(symbol.name != "outside_symbol" for symbol in snapshot.symbols.values())
    assert snapshot.skipped_files >= 1


def test_repo_map_reports_when_file_limit_truncates_scan(
    tmp_path,
    monkeypatch,
):
    for index in range(3):
        (tmp_path / f"module_{index}.py").write_text(
            f"def symbol_{index}():\n    return {index}\n",
            encoding="utf-8",
        )
    monkeypatch.setattr("pico.repo_map.REPO_MAP_MAX_FILES", 1)

    snapshot = RepoMap(Path(tmp_path)).refresh()

    assert snapshot.scan_truncated is True
    assert snapshot.parsed_files == 1
