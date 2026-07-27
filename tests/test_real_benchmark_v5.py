from pathlib import Path

import pytest

from evaluation.real_benchmark import load_real_benchmark
from pico.repo_map import RepoMap


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_PATH = PROJECT_ROOT / "benchmarks" / "real_world_tasks_v5.json"
FIXTURE_ROOT = PROJECT_ROOT / "benchmarks" / "fixtures" / "real_v5" / "ops_center"

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


@pytest.mark.parametrize("task_id", sorted(EXPECTED_ACTIVE_PATHS))
def test_repo_map_finds_the_active_cross_module_path_for_each_interview_fixture(task_id):
    task = next(
        task
        for task in load_real_benchmark(BENCHMARK_PATH, PROJECT_ROOT)["tasks"]
        if task["id"] == task_id
    )
    render = RepoMap(FIXTURE_ROOT).render(
        task["prompt"], budget_tokens=1600, max_results=24
    )
    selected_paths = set(render.details["selected_files"])

    assert EXPECTED_ACTIVE_PATHS[task_id] <= selected_paths
    assert render.details["graph_nodes"] >= 50
    assert render.details["graph_edges"] >= 50
