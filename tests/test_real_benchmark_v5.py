import os
from pathlib import Path
import subprocess
import sys

import pytest

from evaluation.real_benchmark import load_real_benchmark
from pico.repo_map import RepoMap


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_PATH = PROJECT_ROOT / "benchmarks" / "real_world_tasks_v5.json"
FIXTURE_ROOT = (
    PROJECT_ROOT
    / "benchmarks"
    / "fixtures"
    / "real_v5"
    / "ops_center"
)

EXPECTED_ACTIVE_PATHS = {
    "regional_inventory_allocation": {
        "ops_center/inventory/api.py",
        "ops_center/inventory/service.py",
        "ops_center/inventory/policy.py",
        "ops_center/inventory/store.py",
    },
    "cross_midnight_maintenance_window": {
        "ops_center/scheduling/api.py",
        "ops_center/scheduling/service.py",
        "ops_center/scheduling/calendar.py",
        "ops_center/scheduling/timezones.py",
    },
    "discount_rule_precedence": {
        "ops_center/pricing/api.py",
        "ops_center/pricing/service.py",
        "ops_center/pricing/discounts.py",
        "ops_center/pricing/ledger.py",
    },
    "tenant_sticky_rollout_assignment": {
        "ops_center/rollouts/api.py",
        "ops_center/rollouts/service.py",
        "ops_center/rollouts/cohorts.py",
        "ops_center/rollouts/store.py",
    },
    "transitive_incident_blockers": {
        "ops_center/incidents/api.py",
        "ops_center/incidents/service.py",
        "ops_center/incidents/dependencies.py",
        "ops_center/incidents/store.py",
    },
}


def _benchmark():
    return load_real_benchmark(BENCHMARK_PATH, PROJECT_ROOT)


def _pytest_environment():
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(FIXTURE_ROOT)
    return env


def test_v5_is_a_new_shared_multi_package_localization_suite():
    benchmark = _benchmark()
    tasks = benchmark["tasks"]
    fixture_paths = {task["fixture_repo"] for task in tasks}
    production_files = tuple(
        (FIXTURE_ROOT / "ops_center").rglob("*.py")
    )

    assert benchmark["name"] == "pico-repo-map-localization-v5-heldout"
    assert {task["id"] for task in tasks} == set(EXPECTED_ACTIVE_PATHS)
    assert fixture_paths == {
        "benchmarks/fixtures/real_v5/ops_center"
    }
    assert len(production_files) >= 35
    assert (FIXTURE_ROOT / "ops_center" / "legacy").is_dir()
    assert (FIXTURE_ROOT / "ops_center" / "experiments").is_dir()

    for task in tasks:
        assert "query_repo_map" in task["allowed_tools"]
        assert task["step_budget"] == 9
        assert (
            Path(task["verifier_files"][0]["source"]).parts[:3]
            == ("benchmarks", "verifiers", "real_v5")
        )


def test_v5_public_smoke_tests_pass_before_agent_changes():
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
def test_v5_hidden_verifier_rejects_the_unmodified_fixture(task_id):
    task = next(
        task
        for task in _benchmark()["tasks"]
        if task["id"] == task_id
    )
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
def test_v5_repo_map_surfaces_the_active_cross_module_path(task_id):
    task = next(
        task
        for task in _benchmark()["tasks"]
        if task["id"] == task_id
    )
    render = RepoMap(FIXTURE_ROOT).render(
        task["prompt"],
        budget_tokens=1600,
        max_results=24,
    )
    selected_paths = set(render.details["selected_files"])

    assert EXPECTED_ACTIVE_PATHS[task_id] <= selected_paths
    assert any(
        path.startswith(
            ("ops_center/legacy/", "ops_center/experiments/")
        )
        for path in selected_paths
    )
    assert render.details["graph_nodes"] >= 50
    assert render.details["graph_edges"] >= 50
