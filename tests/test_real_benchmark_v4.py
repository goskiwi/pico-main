import os
from pathlib import Path
import subprocess
import sys

import pytest

from evaluation.real_benchmark import load_real_benchmark
from pico.repo_map import RepoMap


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_PATH = PROJECT_ROOT / "benchmarks" / "real_world_tasks_v4.json"
FIXTURE_ROOT = PROJECT_ROOT / "benchmarks" / "fixtures" / "real_v4" / "service_hub"

EXPECTED_ACTIVE_PATHS = {
    "regional_checkout_shipping": {
        "service_hub/checkout/api.py",
        "service_hub/shipping/service.py",
        "service_hub/shipping/policy.py",
    },
    "tenant_scoped_webhook_dedup": {
        "service_hub/webhooks/api.py",
        "service_hub/webhooks/service.py",
        "service_hub/webhooks/keys.py",
        "service_hub/webhooks/store.py",
    },
    "inherited_role_permissions": {
        "service_hub/auth/api.py",
        "service_hub/auth/service.py",
        "service_hub/auth/policy.py",
    },
    "catalog_rename_cache_invalidation": {
        "service_hub/catalog/api.py",
        "service_hub/catalog/service.py",
        "service_hub/catalog/store.py",
        "service_hub/catalog/cache.py",
    },
    "notification_locale_fallback": {
        "service_hub/notifications/api.py",
        "service_hub/notifications/service.py",
        "service_hub/notifications/locale.py",
    },
}


def _benchmark():
    return load_real_benchmark(BENCHMARK_PATH, PROJECT_ROOT)


def _pytest_environment():
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(FIXTURE_ROOT)
    return env


def test_v4_is_a_shared_multi_package_localization_suite():
    benchmark = _benchmark()
    tasks = benchmark["tasks"]
    fixture_paths = {task["fixture_repo"] for task in tasks}
    production_files = tuple((FIXTURE_ROOT / "service_hub").rglob("*.py"))

    assert benchmark["name"] == "pico-repo-map-localization-v4-frozen"
    assert {task["id"] for task in tasks} == set(EXPECTED_ACTIVE_PATHS)
    assert fixture_paths == {"benchmarks/fixtures/real_v4/service_hub"}
    assert len(production_files) >= 35
    assert (FIXTURE_ROOT / "service_hub" / "legacy").is_dir()
    assert (FIXTURE_ROOT / "service_hub" / "experiments").is_dir()

    for task in tasks:
        assert "query_repo_map" in task["allowed_tools"]
        assert 8 <= task["step_budget"] <= 9
        assert (
            Path(task["verifier_files"][0]["source"]).parts[:3]
            == ("benchmarks", "verifiers", "real_v4")
        )


def test_v4_public_smoke_tests_pass_before_agent_changes():
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=FIXTURE_ROOT,
        env=_pytest_environment(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "5 passed" in completed.stdout


@pytest.mark.parametrize("task_id", sorted(EXPECTED_ACTIVE_PATHS))
def test_v4_hidden_verifier_rejects_the_unmodified_fixture(task_id):
    task = next(task for task in _benchmark()["tasks"] if task["id"] == task_id)
    verifier = PROJECT_ROOT / task["verifier_files"][0]["source"]
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-c",
            os.devnull,
            "-p",
            "no:cacheprovider",
            str(verifier),
        ],
        cwd=PROJECT_ROOT,
        env=_pytest_environment(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == 1, output
    assert "failed" in output
    assert "ERROR collecting" not in output
    assert "errors during collection" not in output


@pytest.mark.parametrize("task_id", sorted(EXPECTED_ACTIVE_PATHS))
def test_v4_repo_map_surfaces_the_active_cross_module_path(task_id):
    task = next(task for task in _benchmark()["tasks"] if task["id"] == task_id)
    render = RepoMap(FIXTURE_ROOT).render(
        task["prompt"],
        budget_tokens=1600,
        max_results=24,
    )
    selected_paths = set(render.details["selected_files"])

    assert EXPECTED_ACTIVE_PATHS[task_id] <= selected_paths
    assert any(
        path.startswith(("service_hub/legacy/", "service_hub/experiments/"))
        for path in selected_paths
    )
    assert render.details["graph_nodes"] >= 50
    assert render.details["graph_edges"] >= 50
