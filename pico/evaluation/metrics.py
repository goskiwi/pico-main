"""Small, replayable Runtime quality evaluations."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from ..context_manager import Tokenizer
from ..contracts import FailureInfo, ToolOutcome
from ..project_memory import ProjectMemoryStore
from ..recovery import RecoveryPolicy
from ..repo_map import RepoMap
from ..run_store import RunStore
from ..verification import parse_verification_output
from .provenance import evaluation_snapshot_id, runtime_snapshot_id


def _write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def run_context_governance_ablation(path=Path("artifacts/context-governance-v4.json"), repetitions=3):
    tokenizer = Tokenizer()
    rows = []
    for repetition in range(int(repetitions)):
        for size in (2000, 6000, 12000):
            request = f"critical-request-{repetition}-{size}"
            history = "old noise " * size
            governed = tokenizer.clip(history, 1100) + "\nCurrent user request:\n" + request
            rows.append({
                "repetition": repetition, "raw_tokens": tokenizer.count(history),
                "governed_tokens": tokenizer.count(governed),
                "within_budget": tokenizer.count(governed) <= 1200,
                "request_preserved": request in governed,
            })
    return _write(path, {
        "artifact_type": "context-governance-v4",
        "runtime_snapshot_id": runtime_snapshot_id(),
        "evaluation_snapshot_id": evaluation_snapshot_id(),
        "rows": rows,
        "summary": {
            "within_budget_rate": sum(row["within_budget"] for row in rows) / len(rows),
            "current_request_preserved_rate": sum(row["request_preserved"] for row in rows) / len(rows),
            "mean_token_reduction": sum(row["raw_tokens"] - row["governed_tokens"] for row in rows) / len(rows),
        },
    })


def run_project_memory_evaluation(path=Path("artifacts/project-memory-v1.json")):
    with tempfile.TemporaryDirectory(prefix="pico-project-memory-eval-") as directory:
        root = Path(directory)
        store = ProjectMemoryStore(root / ".pico/memory", root)
        card, _created = store.store(
            action="create", filename="project_test_command.md", name="Test command",
            description="Stable project test workflow", memory_type="project",
            content="Run python -m pytest -q.", why="It is the repository verifier.",
            how_to_apply="Run after code changes.",
            source_session_id="s", source_run_id="r", source_entry_ids=("e1",),
        )
        payload = {
            "artifact_type": "project-memory-v1",
            "runtime_snapshot_id": runtime_snapshot_id(),
            "evaluation_snapshot_id": evaluation_snapshot_id(),
            "summary": {
                "markdown_source_of_truth": (store.cards_root / card.filename).is_file(),
                "index_generated": card.filename in store.index_text(),
                "catalog_generated": card.filename in store.index_text(),
                "provenance_complete": bool(card.source_entry_ids and card.source_run_id),
            },
        }
    return _write(path, payload)


def run_repo_map_evaluation(path=Path("artifacts/repo-map-v1.json")):
    with tempfile.TemporaryDirectory(prefix="pico-repomap-eval-") as directory:
        root = Path(directory)
        (root / "service.py").write_text(
            "def load_config():\n    return 1\n\ndef start():\n    return load_config()\n", encoding="utf-8"
        )
        result = RepoMap(root).render("where is config loaded", budget_tokens=300, max_results=10)
        payload = {
            "artifact_type": "repo-map-v1",
            "runtime_snapshot_id": runtime_snapshot_id(),
            "evaluation_snapshot_id": evaluation_snapshot_id(),
            "summary": {
                "query_hit": "load_config" in result.text,
                "within_budget": Tokenizer().count(result.text) <= 300,
                "index_revision_bound": result.details["index_revision"].startswith("sha256:"),
            },
            "details": result.details,
        }
    return _write(path, payload)


def run_runtime_policy_evaluation(path=Path("artifacts/runtime-policy-v1.json")):
    with tempfile.TemporaryDirectory(prefix="pico-runtime-eval-") as directory:
        store = RunStore(Path(directory) / "runs")
        store.append_entry(
            "run_eval", "task_eval", "session_eval", "run_started", {"user_request": "repair"}
        )
        policy = RecoveryPolicy()
        outcome = ToolOutcome(
            "call_eval", "run_shell", "error", "failed", "none", "exit_code: 1",
            "same-call", {"status": "admitted", "stages": []},
            failure=FailureInfo("tool_failed", "command", "same failure", True),
            workspace_fingerprint="workspace-eval",
        )
        first = policy.assess(
            outcome.failure,
            status=outcome.status,
            fingerprint=outcome.call_fingerprint,
            scope="run_eval",
        )
        second = policy.assess(
            outcome.failure,
            status=outcome.status,
            fingerprint=outcome.call_fingerprint,
            scope="run_eval",
        )
        for decision in (first, second):
            store.append_entry(
                "run_eval",
                "task_eval",
                "session_eval",
                "policy_decided",
                {
                    "stop": decision.action == "stop",
                    "reason": decision.reason,
                    "guidance": "\n".join(decision.guidance),
                },
            )
        entries = store.read_entries("run_eval")
        verification = parse_verification_output(
            "python -m pytest -q", "FAILED tests/test_x.py::test_x\n1 failed, 2 passed", 1
        )
        payload = {
            "artifact_type": "runtime-policy-v1",
            "runtime_snapshot_id": runtime_snapshot_id(),
            "evaluation_snapshot_id": evaluation_snapshot_id(),
            "summary": {
                "journal_valid": len(entries) == 3,
                "replayable_policy": store.replay("run_eval").summary()["policy_counts"].get("continue") == 2,
                "repeated_failure_replanned": second.action == "replan",
                "verification_structured": verification["failed_tests"] == ["tests/test_x.py::test_x"],
            },
        }
    return _write(path, payload)


def write_runtime_report(
    path=Path("docs/metrics/runtime-evaluation.md"),
    context_path=Path("artifacts/context-governance-v4.json"),
    project_memory_path=Path("artifacts/project-memory-v1.json"),
    repo_map_path=Path("artifacts/repo-map-v1.json"),
    runtime_policy_path=Path("artifacts/runtime-policy-v1.json"),
    harness_path=Path("artifacts/harness-regression-v3.json"),
):
    harness = json.loads(Path(harness_path).read_text())
    context = json.loads(Path(context_path).read_text())
    project = json.loads(Path(project_memory_path).read_text())
    repo = json.loads(Path(repo_map_path).read_text())
    policy_path = Path(runtime_policy_path)
    policy = json.loads(policy_path.read_text()) if policy_path.exists() else None
    text = "\n".join([
        "# Pico Runtime Evaluation", "",
        "These deterministic artifacts measure Runtime mechanisms, not model intelligence.", "",
        "## Native Harness regression", "",
        f"- Passed: {harness['summary']['passed']}/{harness['summary']['total_tasks']}",
        f"- Verifier pass rate: {harness['summary']['verifier_pass_rate']:.1%}",
        f"- Within-budget rate: {harness['summary']['within_budget_rate']:.1%}", "",
        "## Context governance", "",
        f"- Within-budget rate: {context['summary']['within_budget_rate']:.1%}",
        f"- Current-request preservation: {context['summary']['current_request_preserved_rate']:.1%}", "",
        "## Project memory", "",
        f"- Catalog generated: {project['summary']['catalog_generated']}", "",
        "## RepoMap", "",
        f"- Query hit: {repo['summary']['query_hit']}",
        f"- Within budget: {repo['summary']['within_budget']}", "",
    ])
    if policy:
        text += "\n" + "\n".join([
            "## Runtime policy", "",
            f"- Run Journal valid: {policy['summary']['journal_valid']}",
            f"- Repeated failure replanned: {policy['summary']['repeated_failure_replanned']}",
            f"- Structured verification: {policy['summary']['verification_structured']}", "",
        ])
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text
