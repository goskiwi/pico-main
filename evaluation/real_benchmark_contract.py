"""Stable constants shared by the real-model benchmark modules."""

from pathlib import Path


REAL_BENCHMARK_SCHEMA_VERSION = 1
REAL_BENCHMARK_ARTIFACT_SCHEMA_VERSION = 4
DEFAULT_REAL_BENCHMARK_PATH = Path("benchmarks/real_world_tasks_v5.json")
DEFAULT_REAL_ARTIFACT_PATH = Path("artifacts/real-world-benchmark.json")
DEFAULT_REAL_REPORT_PATH = Path("artifacts/real-world-benchmark.md")
DEFAULT_REAL_WORKSPACE_ROOT = Path("artifacts/real-world-workspaces")
REQUIRED_TASK_KEYS = (
    "id",
    "category",
    "prompt",
    "fixture_repo",
    "allowed_tools",
    "step_budget",
    "verifier_files",
    "verifier_command",
)
VARIANT_FULL = "full"
VARIANT_REPO_MAP_600 = "repo_map_600"
VARIANT_REPO_MAP_1000 = "repo_map_1000"
VARIANT_REPO_MAP_1600 = "repo_map_1600"
VARIANT_NO_MEMORY_CONTEXT = "no_memory_context"
VARIANT_NO_REPO_MAP = "no_repo_map"
REPO_MAP_BUDGET_VARIANTS = {
    VARIANT_REPO_MAP_600: 600,
    VARIANT_REPO_MAP_1000: 1000,
    VARIANT_REPO_MAP_1600: 1600,
}
SUPPORTED_VARIANTS = (
    VARIANT_FULL,
    *REPO_MAP_BUDGET_VARIANTS,
    VARIANT_NO_MEMORY_CONTEXT,
    VARIANT_NO_REPO_MAP,
)
