"""Small, replayable Runtime quality evaluations."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from pico.context_manager import ContextManager, Tokenizer
from pico.contracts import ModelAction, ToolCall, ToolOutcome
from pico.providers.clients import FakeModelClient
from pico.repo_map import RepoMap
from pico.run_lifecycle import RunLifecycle
from pico.runtime import Pico
from pico.runtime_config import PicoConfig
from pico.session_store import SessionStore
from pico.workspace import WorkspaceContext


def _write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def run_context_governance_evaluation(
    path=Path("artifacts/context-governance.json"),
):
    rows = []
    with tempfile.TemporaryDirectory(prefix="pico-context-eval-") as directory:
        root = Path(directory)
        for size in (2000, 6000, 12000):
            task_root = root / f"size-{size}"
            task_root.mkdir(parents=True)
            (task_root / "README.md").write_text("context evaluation\n")
            request = f"critical-request-{size}"
            agent = Pico(
                FakeModelClient([]),
                WorkspaceContext.build(task_root),
                SessionStore(task_root / ".pico" / "sessions"),
                config=PicoConfig(
                    approval_policy="auto",
                    max_new_tokens=64,
                    provider_context_limit_tokens=1200,
                    compaction_reserve_tokens=200,
                    compaction_keep_recent_tokens=300,
                    verification_command="",
                ),
            )
            RunLifecycle(agent).initialize(
                request,
                task_kind="read_only",
                requires_workspace_change=False,
                requires_verification=False,
            )
            run_log = agent.run.run_log
            chunk = "old noise " * max(1, size // 8)
            for index in range(8):
                call = ToolCall(
                    "read_file",
                    {"path": "README.md", "start_line": 1, "end_line": 1},
                    f"call_{size}_{index}",
                )
                agent.apply_run_event(run_log.append_tool_call(call))
                agent.apply_run_event(
                    run_log.append_tool_started(
                        call,
                        risky=False,
                        effect_scope="none",
                        potential_effects=[],
                    )
                )
                agent.apply_run_event(
                    run_log.append_tool_result(
                        ToolOutcome(
                            tool_call_id=call.call_id,
                            tool_name=call.name,
                            status="success",
                            execution_state="completed",
                            side_effect_state="none",
                            content=chunk,
                        ),
                    )
                )

            manager = ContextManager(
                agent,
                total_budget=1200,
                section_caps={
                    "workspace": 300,
                    "task_requirements": 80,
                    "memory_catalog": 60,
                    "repo_map": 80,
                    "working_state": 100,
                },
                compaction_reserve_tokens=200,
                compaction_keep_recent_tokens=300,
            )
            raw_history_tokens = manager.tokenizer.count(
                manager._history_text(request)
            )
            original_event_ids = {event.event_id for event in run_log.events}
            compaction, history_override = manager.prepare_compaction(request)
            prompt, metadata = manager.build(
                request,
                compaction_metadata=compaction,
                history_override=history_override,
            )
            active = run_log.active_events()
            active_calls = {
                event.call_id
                for event in active
                if event.kind == "assistant_tool_call"
            }
            active_results = {
                event.call_id for event in active if event.kind == "tool_result"
            }
            governed_history_tokens = metadata["sections"]["history"][
                "rendered_tokens"
            ]
            rows.append(
                {
                    "size": size,
                    "raw_tokens": raw_history_tokens,
                    "governed_tokens": governed_history_tokens,
                    "within_budget": metadata["within_budget"],
                    "request_preserved": request in prompt,
                    "compaction_committed": metadata["compaction"] is not None,
                    "tool_transactions_intact": active_calls == active_results,
                    "original_events_preserved": original_event_ids
                    <= {event.event_id for event in run_log.events},
                    "working_state_preserved": (
                        agent.run.task.contract.goal == request
                    ),
                }
            )
    return _write(path, {
        "artifact_type": "context-governance",
        "rows": rows,
        "summary": {
            "within_budget_rate": sum(row["within_budget"] for row in rows) / len(rows),
            "current_request_preserved_rate": sum(row["request_preserved"] for row in rows) / len(rows),
            "compaction_commit_rate": sum(row["compaction_committed"] for row in rows) / len(rows),
            "tool_transaction_integrity_rate": sum(row["tool_transactions_intact"] for row in rows) / len(rows),
            "original_event_preservation_rate": sum(row["original_events_preserved"] for row in rows) / len(rows),
            "working_state_preservation_rate": sum(row["working_state_preserved"] for row in rows) / len(rows),
            "mean_token_reduction": sum(row["raw_tokens"] - row["governed_tokens"] for row in rows) / len(rows),
        },
    })


def run_project_memory_evaluation(path=Path("artifacts/project-memory.json")):
    with tempfile.TemporaryDirectory(prefix="pico-project-memory-eval-") as directory:
        root = Path(directory)
        (root / "README.md").write_text("project memory evaluation\n")
        agent = Pico(
            FakeModelClient(
                [
                    ModelAction.tool(
                        "memory_store",
                        {
                            "action": "create",
                            "filename": "project_test_command.md",
                            "name": "Test command",
                            "description": "Stable project test workflow",
                            "memory_type": "project",
                            "content": "Run python -m pytest -q.",
                            "why": "It is the repository verifier.",
                            "how_to_apply": "Run after code changes.",
                        },
                    ),
                    ModelAction.tool(
                        "memory_recall",
                        {"filenames": ["project_test_command.md"]},
                    ),
                    ModelAction.final("Stored and recalled project memory."),
                ]
            ),
            WorkspaceContext.build(root),
            SessionStore(root / ".pico" / "sessions"),
            config=PicoConfig(approval_policy="auto", verification_command=""),
        )
        answer = agent.ask(
            "Remember and recall the stable project test command.",
            task_kind="modify",
            requires_workspace_change=False,
            requires_verification=False,
        )
        card = agent.dependencies.project_memory.recall("project_test_command.md")
        events = agent.run.run_log.events
        store_call = next(
            event
            for event in events
            if event.kind == "assistant_tool_call" and event.name == "memory_store"
        )
        recall_call = next(
            event
            for event in events
            if event.kind == "assistant_tool_call" and event.name == "memory_recall"
        )
        results = {
            event.call_id: event
            for event in events
            if event.kind == "tool_result"
        }
        recall_result = results[recall_call.call_id]
        payload = {
            "artifact_type": "project-memory",
            "summary": {
                "markdown_source_of_truth": (
                    agent.dependencies.project_memory.cards_root / card.filename
                ).is_file(),
                "catalog_generated": card.filename
                in agent.dependencies.project_memory.index_text(),
                "store_transaction_recorded": results[store_call.call_id].outcome_status
                == "success",
                "recall_transaction_recorded": recall_result.outcome_status == "success",
                "recalled_body_returned": "Run python -m pytest -q."
                in recall_result.content,
                "untrusted_boundary_rendered": 'trust="untrusted_data"'
                in recall_result.content,
                "write_provenance_recorded": bool(
                    card.source_run_id
                    and card.source_tool_call_id == store_call.call_id
                ),
                "run_completed": agent.run.task.lifecycle.status == "completed"
                and answer == "Stored and recalled project memory.",
                "no_pending_call": agent.dependencies.run_store.replay(
                    agent.run.projection.run_id
                ).summary()["pending_call_id"]
                is None,
            },
        }
    return _write(path, payload)


def run_repo_map_evaluation(path=Path("artifacts/repo-map.json")):
    with tempfile.TemporaryDirectory(prefix="pico-repomap-eval-") as directory:
        root = Path(directory)
        (root / "service.py").write_text(
            "def load_config():\n    return 1\n\ndef start():\n    return load_config()\n", encoding="utf-8"
        )
        result = RepoMap(root).render("where is config loaded", budget_tokens=300, max_results=10)
        payload = {
            "artifact_type": "repo-map",
            "summary": {
                "query_hit": "load_config" in result.text,
                "within_budget": Tokenizer().count(result.text) <= 300,
            },
            "details": result.details,
        }
    return _write(path, payload)


def write_runtime_report(
    path=Path("docs/metrics/runtime-evaluation.md"),
    context_path=Path("artifacts/context-governance.json"),
    project_memory_path=Path("artifacts/project-memory.json"),
    repo_map_path=Path("artifacts/repo-map.json"),
    harness_path=Path("artifacts/harness-regression.json"),
):
    harness = json.loads(Path(harness_path).read_text())
    context = json.loads(Path(context_path).read_text())
    project = json.loads(Path(project_memory_path).read_text())
    repo = json.loads(Path(repo_map_path).read_text())
    text = "\n".join([
        "# Pico Runtime Evaluation", "",
        "These deterministic artifacts measure Runtime mechanisms, not model intelligence.", "",
        "## Native Harness regression", "",
        f"- Passed: {harness['summary']['passed']}/{harness['summary']['total_tasks']}",
        f"- Verifier pass rate: {harness['summary']['verifier_pass_rate']:.1%}",
        f"- Within-budget rate: {harness['summary']['within_budget_rate']:.1%}", "",
        "## Context governance", "",
        f"- Within-budget rate: {context['summary']['within_budget_rate']:.1%}",
        f"- Current-request preservation: {context['summary']['current_request_preserved_rate']:.1%}",
        f"- Compaction commit rate: {context['summary']['compaction_commit_rate']:.1%}",
        f"- Tool-transaction integrity: {context['summary']['tool_transaction_integrity_rate']:.1%}",
        f"- Original-event preservation: {context['summary']['original_event_preservation_rate']:.1%}",
        f"- WorkingState preservation: {context['summary']['working_state_preservation_rate']:.1%}", "",
        "## Project memory", "",
        f"- Catalog generated: {project['summary']['catalog_generated']}",
        f"- Explicit Store transaction: {project['summary']['store_transaction_recorded']}",
        f"- Explicit Recall transaction: {project['summary']['recall_transaction_recorded']}",
        f"- Untrusted-data boundary: {project['summary']['untrusted_boundary_rendered']}", "",
        "## RepoMap", "",
        f"- Query hit: {repo['summary']['query_hit']}",
        f"- Within budget: {repo['summary']['within_budget']}", "",
    ])
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text
