"""Small, replayable Runtime quality evaluations."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from ..context_manager import Tokenizer
from ..contracts import FailureInfo, ToolOutcome
from ..features.memory import SessionWorkingMemory
from ..progress import ProgressGovernor
from ..project_memory import ProjectMemoryStore
from ..repo_map import RepoMap
from ..run_store import RunStore
from ..verification import parse_verification_output
from .provenance import evaluation_snapshot_id, runtime_snapshot_id


def _write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def run_context_governance_ablation(path=Path("artifacts/context-governance-v3.json"), repetitions=3):
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
        "artifact_type": "context-governance-v3",
        "runtime_snapshot_id": runtime_snapshot_id(),
        "evaluation_snapshot_id": evaluation_snapshot_id(),
        "rows": rows,
        "summary": {
            "within_budget_rate": sum(row["within_budget"] for row in rows) / len(rows),
            "current_request_preserved_rate": sum(row["request_preserved"] for row in rows) / len(rows),
            "mean_token_reduction": sum(row["raw_tokens"] - row["governed_tokens"] for row in rows) / len(rows),
        },
    })


def run_working_memory_ablation(path=Path("artifacts/working-memory-v3.json"), repetitions=3):
    rows = []
    for repetition in range(int(repetitions)):
        with tempfile.TemporaryDirectory(prefix="pico-memory-eval-") as directory:
            root = Path(directory)
            target = root / "config.py"
            target.write_text("COLOR = 'red'\n", encoding="utf-8")
            memory = SessionWorkingMemory(workspace_root=root)
            memory.set_goal("find color").remember_file("config.py").set_file_observation(
                "config.py", "COLOR is red", source_session_id="s",
                source_run_id=f"r{repetition}", source_tool_call_id="c", source_artifact_id="a",
            )
            recalled, metadata = memory.render_recall("what is COLOR")
            rows.append({"variant": "memory_on", "hit": "COLOR is red" in recalled,
                         "repeated_reads": 0 if metadata["working_entry_ids"] else 1})
            target.write_text("COLOR = 'blue'\n", encoding="utf-8")
            stale, metadata = memory.render_recall("what is COLOR")
            rows.append({"variant": "stale_revision", "hit": "COLOR is red" in stale,
                         "repeated_reads": 1})
            rows.append({"variant": "memory_off", "hit": False, "repeated_reads": 1})
    variants = {}
    for variant in ("memory_on", "stale_revision", "memory_off"):
        selected = [row for row in rows if row["variant"] == variant]
        variants[variant] = {
            "hit_rate": sum(row["hit"] for row in selected) / len(selected),
            "mean_repeated_reads": sum(row["repeated_reads"] for row in selected) / len(selected),
        }
    return _write(path, {
        "artifact_type": "working-memory-v3",
        "runtime_snapshot_id": runtime_snapshot_id(),
        "evaluation_snapshot_id": evaluation_snapshot_id(),
        "rows": rows,
        "variants": variants,
    })


def run_project_memory_evaluation(path=Path("artifacts/project-memory-v1.json")):
    with tempfile.TemporaryDirectory(prefix="pico-project-memory-eval-") as directory:
        root = Path(directory)
        store = ProjectMemoryStore(root / ".pico/memory", root)
        card, _created = store.store(
            action="create", filename="project_test_command.md", name="Test command",
            description="Stable project test workflow", memory_type="project",
            content="Run python -m pytest -q.", why="It is the repository verifier.",
            how_to_apply="Run after code changes.", origin="explicit",
            source_session_id="s", source_run_id="r", source_entry_ids=("e1",),
        )
        kept, automatic_action = store.store(
            action="update", filename=card.filename, name=card.name,
            description=card.description, memory_type=card.type, content="unsafe overwrite",
            why=card.why, how_to_apply=card.how_to_apply, origin="automatic",
            source_session_id="s2", source_run_id="r2", source_entry_ids=("e2",),
        )
        payload = {
            "artifact_type": "project-memory-v1",
            "runtime_snapshot_id": runtime_snapshot_id(),
            "evaluation_snapshot_id": evaluation_snapshot_id(),
            "summary": {
                "markdown_source_of_truth": (store.cards_root / card.filename).is_file(),
                "index_generated": card.filename in store.index_text(),
                "explicit_precedence": automatic_action == "kept_explicit" and kept.content == card.content,
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


def run_runtime_governance_evaluation(path=Path("artifacts/runtime-governance-v1.json")):
    with tempfile.TemporaryDirectory(prefix="pico-runtime-eval-") as directory:
        store = RunStore(Path(directory) / "runs")
        store.append_event("run_eval", "task_eval", "run_started", {"user_request": "repair"})
        governor = ProgressGovernor()
        outcome = ToolOutcome(
            "call_eval", "run_shell", "error", "failed", "none", "exit_code: 1",
            "same-call", {"status": "admitted", "stages": []},
            failure=FailureInfo("tool_failed", "command", "same failure", True),
            workspace_fingerprint="workspace-eval",
        )
        first = governor.observe_tool(outcome)
        second = governor.observe_tool(outcome)
        for decision in (first, second):
            store.append_event("run_eval", "task_eval", "progress_decided", decision.to_dict())
        events = store.read_events("run_eval")
        verification = parse_verification_output(
            "python -m pytest -q", "FAILED tests/test_x.py::test_x\n1 failed, 2 passed", 1
        )
        payload = {
            "artifact_type": "runtime-governance-v1",
            "runtime_snapshot_id": runtime_snapshot_id(),
            "evaluation_snapshot_id": evaluation_snapshot_id(),
            "summary": {
                "hash_chain_valid": len(events) == 3,
                "replayable_progress": store.replay("run_eval").summary()["progress_counts"].get("replan") == 1,
                "repeated_failure_replanned": second.decision == "replan",
                "verification_structured": verification["failed_tests"] == ["tests/test_x.py::test_x"],
            },
        }
    return _write(path, payload)


def write_runtime_report(
    path=Path("docs/metrics/runtime-evaluation.md"),
    context_path=Path("artifacts/context-governance-v3.json"),
    working_memory_path=Path("artifacts/working-memory-v3.json"),
    project_memory_path=Path("artifacts/project-memory-v1.json"),
    repo_map_path=Path("artifacts/repo-map-v1.json"),
    runtime_governance_path=Path("artifacts/runtime-governance-v1.json"),
    harness_path=Path("artifacts/harness-regression-v3.json"),
):
    harness = json.loads(Path(harness_path).read_text())
    context = json.loads(Path(context_path).read_text())
    working = json.loads(Path(working_memory_path).read_text())
    project = json.loads(Path(project_memory_path).read_text())
    repo = json.loads(Path(repo_map_path).read_text())
    governance_path = Path(runtime_governance_path)
    governance = json.loads(governance_path.read_text()) if governance_path.exists() else None
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
        "## Working memory", "",
        f"- Fresh recall hit rate: {working['variants']['memory_on']['hit_rate']:.1%}",
        f"- Stale recall hit rate: {working['variants']['stale_revision']['hit_rate']:.1%}", "",
        "## Project memory", "",
        f"- Explicit precedence: {project['summary']['explicit_precedence']}", "",
        "## RepoMap", "",
        f"- Query hit: {repo['summary']['query_hit']}",
        f"- Within budget: {repo['summary']['within_budget']}", "",
    ])
    if governance:
        text += "\n" + "\n".join([
            "## Runtime governance", "",
            f"- Hash chain valid: {governance['summary']['hash_chain_valid']}",
            f"- Repeated failure replanned: {governance['summary']['repeated_failure_replanned']}",
            f"- Structured verification: {governance['summary']['verification_structured']}", "",
        ])
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text
