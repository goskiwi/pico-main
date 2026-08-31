"""Small, replayable Runtime quality evaluations."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from pico.context_manager import Tokenizer, _ContextAssembler
from pico.contracts import ToolCall, ToolOutcome
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
                task_intent="read_only",
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

            manager = _ContextAssembler(
                agent,
                total_budget=1200,
                section_caps={
                    "workspace": 300,
                    "repository_conventions": 80,
                    "repo_map": 80,
                    "working_state": 100,
                },
                compaction_reserve_tokens=200,
                compaction_keep_recent_tokens=300,
            )
            raw_history_tokens = manager.tokenizer.count(
                manager._history_text()
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
                    "task_request_preserved": request in prompt,
                    "compaction_committed": metadata["compaction"] is not None,
                    "tool_transactions_intact": active_calls == active_results,
                    "original_events_preserved": original_event_ids
                    <= {event.event_id for event in run_log.events},
                    "task_contract_preserved": (
                        agent.run.task.contract.goal == request
                    ),
                }
            )
    return _write(path, {
        "artifact_type": "context-governance",
        "rows": rows,
        "summary": {
            "within_budget_rate": sum(row["within_budget"] for row in rows) / len(rows),
            "task_request_preserved_rate": sum(
                row["task_request_preserved"] for row in rows
            ) / len(rows),
            "compaction_commit_rate": sum(row["compaction_committed"] for row in rows) / len(rows),
            "tool_transaction_integrity_rate": sum(row["tool_transactions_intact"] for row in rows) / len(rows),
            "original_event_preservation_rate": sum(row["original_events_preserved"] for row in rows) / len(rows),
            "task_contract_preservation_rate": sum(
                row["task_contract_preserved"] for row in rows
            ) / len(rows),
            "mean_token_reduction": sum(row["raw_tokens"] - row["governed_tokens"] for row in rows) / len(rows),
        },
    })


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
    repo_map_path=Path("artifacts/repo-map.json"),
    harness_path=Path("artifacts/harness-regression.json"),
):
    harness = json.loads(Path(harness_path).read_text())
    context = json.loads(Path(context_path).read_text())
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
        f"- Task-request preservation: {context['summary']['task_request_preserved_rate']:.1%}",
        f"- Compaction commit rate: {context['summary']['compaction_commit_rate']:.1%}",
        f"- Tool-transaction integrity: {context['summary']['tool_transaction_integrity_rate']:.1%}",
        f"- Original-event preservation: {context['summary']['original_event_preservation_rate']:.1%}",
        f"- TaskContract preservation: {context['summary']['task_contract_preservation_rate']:.1%}", "",
        "## RepoMap", "",
        f"- Query hit: {repo['summary']['query_hit']}",
        f"- Within budget: {repo['summary']['within_budget']}", "",
    ])
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text
