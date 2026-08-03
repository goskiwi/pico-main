"""Stable contract for Pico's live multi-turn continuation benchmark."""

from pathlib import Path


CONTINUATION_BENCHMARK_SCHEMA_VERSION = 1
CONTINUATION_ARTIFACT_SCHEMA_VERSION = 1

DEFAULT_CONTINUATION_BENCHMARK_PATH = Path(
    "benchmarks/live_continuation_tasks_v1.json"
)
DEFAULT_CONTINUATION_ARTIFACT_PATH = Path(
    "artifacts/live-continuation-benchmark-v1-3x.json"
)
DEFAULT_CONTINUATION_REPORT_PATH = Path(
    "docs/metrics/live-continuation-benchmark-v1-3x.md"
)
DEFAULT_CONTINUATION_WORKSPACE_ROOT = Path(
    "artifacts/live-continuation-workspaces"
)
HIDDEN_VERIFIER_SOURCE = Path(
    "benchmarks/verifiers/live_continuation_v1/verify_output.py"
)
HIDDEN_VERIFIER_TARGET = Path(".benchmark_hidden/verify_output.py")

VARIANT_WORKING_MEMORY = "working_memory"
VARIANT_MEMORY_DISABLED = "memory_disabled"
MEMORY_VARIANTS = (
    VARIANT_WORKING_MEMORY,
    VARIANT_MEMORY_DISABLED,
)

RESUME_STATUSES = (
    "full-valid",
    "partial-stale",
    "workspace-mismatch",
    "schema-mismatch",
)

MEMORY_REQUIRED_KEYS = (
    "id",
    "category",
    "fixture_repo",
    "source_path",
    "phase_one_prompt",
    "phase_two_prompt",
    "allowed_tools",
    "step_budget",
    "expected_output",
)
RESUME_REQUIRED_KEYS = (
    *MEMORY_REQUIRED_KEYS,
    "expected_resume_status",
    "expected_stale_paths",
    "expected_runtime_identity_mismatch_fields",
    "phase_two_source_read_requirement",
    "mutation",
)

MUTATION_TYPES = (
    "none",
    "replace_file",
    "delete_file",
    "append_file",
    "set_checkpoint_schema",
)
SOURCE_READ_REQUIREMENTS = (
    "none",
    "attempted",
    "successful",
)
