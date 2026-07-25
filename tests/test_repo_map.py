from pico.context_manager import ContextManager
from pico.repo_map import RepoMap
from tests.helpers import build_agent


def _write_python_repo(root):
    (root / "app").mkdir()
    (root / "tests").mkdir()
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "models.py").write_text(
        """
class User:
    def save(self):
        return "saved"
""".lstrip(),
        encoding="utf-8",
    )
    (root / "app" / "services.py").write_text(
        """
from app.models import User

class UserService:
    def create_user(self):
        user = User()
        user.save()
        return user
""".lstrip(),
        encoding="utf-8",
    )
    (root / "tests" / "test_services.py").write_text(
        """
from app.services import UserService

def test_create_user_saves_once():
    assert UserService().create_user()
""".lstrip(),
        encoding="utf-8",
    )


def test_repo_map_ranks_task_symbols_and_cross_file_relations(tmp_path):
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
    assert result.details["graph_nodes"] >= 8
    assert result.details["graph_edges"] >= 8
    assert result.details["selected_symbols"][0]["qualified_name"] in {
        "UserService",
        "UserService.create_user",
        "test_create_user_saves_once",
    }


def test_repo_map_cache_reparses_only_changed_files(tmp_path):
    _write_python_repo(tmp_path)
    repo_map = RepoMap(tmp_path)

    first = repo_map.render("create_user", budget_tokens=500)
    second = repo_map.render("create_user", budget_tokens=500)
    services = tmp_path / "app" / "services.py"
    services.write_text(
        services.read_text(encoding="utf-8")
        + "\ndef normalize_user_name(name):\n    return name.strip().lower()\n",
        encoding="utf-8",
    )
    third = repo_map.render("normalize_user_name", budget_tokens=500)

    assert first.details["cache_misses"] == 4
    assert first.details["cache_hits"] == 0
    assert second.details["cache_hits"] == 4
    assert second.details["cache_misses"] == 0
    assert third.details["cache_hits"] == 3
    assert third.details["cache_misses"] == 1
    assert "normalize_user_name" in third.text


def test_repo_map_enforces_budget_and_skips_generated_directories(tmp_path):
    _write_python_repo(tmp_path)
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "generated.py").write_text(
        "def generated_symbol():\n    return 1\n",
        encoding="utf-8",
    )

    result = RepoMap(tmp_path).render(
        "create_user",
        budget_tokens=90,
        max_results=60,
    )

    assert "generated_symbol" not in result.text
    assert result.details["parsed_files"] == 4
    assert result.details["skipped_files"] == 1
    assert result.details["truncated"] is True


def test_repo_map_does_not_fill_budget_from_unrelated_graph_components(tmp_path):
    _write_python_repo(tmp_path)
    (tmp_path / "unrelated.py").write_text(
        """
class MetricsCache:
    def get(self, key):
        return key

    def update(self, key):
        return self.get(key)
""".lstrip(),
        encoding="utf-8",
    )

    result = RepoMap(tmp_path).render(
        "Fix UserService.create_user duplicate save",
        budget_tokens=1200,
        max_results=60,
    )

    assert "UserService.create_user" in result.text
    assert "MetricsCache" not in result.text


def test_repo_map_reports_tree_sitter_parse_errors_without_failing(tmp_path):
    (tmp_path / "broken.py").write_text(
        "def broken(:\n    return 1\n",
        encoding="utf-8",
    )

    result = RepoMap(tmp_path).render("broken", budget_tokens=300)

    assert result.details["parsed_files"] == 1
    assert result.details["parse_error_files"] == 1
    assert "Repository map" in result.text


def test_context_manager_injects_ranked_repo_map_and_metadata(tmp_path):
    _write_python_repo(tmp_path)
    agent = build_agent(tmp_path, [])

    prompt, metadata = ContextManager(agent).build(
        "Fix UserService.create_user duplicate save"
    )

    assert prompt.index("Repository map") < prompt.index("Relevant memory:")
    assert "UserService.create_user" in prompt
    assert metadata["repo_map"]["enabled"] is True
    assert metadata["repo_map"]["selected_count"] > 0
    assert "app/services.py" in metadata["repo_map"]["selected_files"]
    assert metadata["dynamic_adjustment"]["strategy"] == "repo_map_boost"


def test_context_manager_enforces_explicit_repo_map_budget_cap(tmp_path):
    _write_python_repo(tmp_path)
    agent = build_agent(tmp_path, [])
    agent.repo_map_budget_tokens = 600
    manager = ContextManager(agent)

    _, metadata = manager.build("Fix UserService.create_user in app/services.py")

    assert metadata["section_budgets_tokens"]["repo_map"] == 600
    assert metadata["sections"]["repo_map"]["budget_tokens"] == 600
    assert metadata["repo_map"]["rendered_tokens"] <= 600
    assert metadata["dynamic_adjustment"]["strategy"] == "repo_map_boost"
    assert metadata["dynamic_adjustment"]["repo_map_budget_cap_tokens"] == 600
    assert (
        metadata["dynamic_adjustment"]["repo_map_budget_before_cap_tokens"]
        > 600
    )


def test_query_repo_map_tool_returns_ranked_symbols_and_cache_evidence(tmp_path):
    _write_python_repo(tmp_path)
    agent = build_agent(tmp_path, [])

    result = agent.run_tool(
        "query_repo_map",
        {
            "query": "UserService create_user tests",
            "budget_tokens": 600,
            "max_results": 12,
        },
    )

    assert "UserService.create_user" in result
    assert "test_create_user_saves_once" in result
    assert "repo_map_stats:" in result
    assert "selected=" in result


def test_disabling_repo_map_removes_context_and_query_tool(tmp_path):
    _write_python_repo(tmp_path)
    agent = build_agent(
        tmp_path,
        [],
        feature_flags={"repo_map": False},
    )

    prompt, metadata = ContextManager(agent).build("Fix UserService.create_user")

    assert "query_repo_map" not in agent.tools
    assert "Repository map:\n- disabled" in prompt
    assert metadata["repo_map"]["enabled"] is False
