"""Direct contracts for tokenizer budgeting and Python repository understanding."""

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
        "from app.services import UserService\n\n"
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
    assert result.details["index_revision"].startswith("sha256:")


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
