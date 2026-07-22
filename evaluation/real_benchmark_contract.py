"""Stable constants shared by the real-model benchmark modules."""

from pathlib import Path


REAL_BENCHMARK_SCHEMA_VERSION = 1
REAL_BENCHMARK_ARTIFACT_SCHEMA_VERSION = 3
DEFAULT_REAL_BENCHMARK_PATH = Path("benchmarks/real_world_tasks.json")
DEFAULT_REAL_ARTIFACT_PATH = Path("artifacts/real-world-benchmark-v1-structured.json")
DEFAULT_REAL_REPORT_PATH = Path("docs/metrics/real-world-benchmark-v1-structured.md")
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
VARIANT_NO_MEMORY_CONTEXT = "no_memory_context"
SUPPORTED_VARIANTS = (VARIANT_FULL, VARIANT_NO_MEMORY_CONTEXT)
