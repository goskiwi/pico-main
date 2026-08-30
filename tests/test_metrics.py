import json

from evals.metrics import (
    run_context_governance_evaluation,
    run_project_memory_evaluation,
    run_repo_map_evaluation,
    write_runtime_report,
)


def test_context_governance_evaluation_uses_real_runtime(tmp_path):
    artifact = run_context_governance_evaluation(
        tmp_path / "context.json",
    )
    assert artifact["summary"]["within_budget_rate"] == 1.0
    assert artifact["summary"]["task_request_preserved_rate"] == 1.0
    assert artifact["summary"]["mean_token_reduction"] > 0
    assert artifact["summary"]["compaction_commit_rate"] == 1.0
    assert artifact["summary"]["tool_transaction_integrity_rate"] == 1.0
    assert artifact["summary"]["original_event_preservation_rate"] == 1.0
    assert artifact["summary"]["task_contract_preservation_rate"] == 1.0
    assert len(artifact["rows"]) == 3


def test_project_memory_and_repo_map_evaluations(tmp_path):
    project = run_project_memory_evaluation(tmp_path / "project.json")
    repo = run_repo_map_evaluation(tmp_path / "repo.json")
    assert all(project["summary"].values())
    assert repo["summary"] == {
        "query_hit": True,
        "within_budget": True,
    }


def test_runtime_report_uses_replayable_artifacts(tmp_path):
    context = tmp_path / "context.json"
    project = tmp_path / "project.json"
    repo = tmp_path / "repo.json"
    harness = tmp_path / "harness.json"
    harness.write_text(json.dumps({"summary": {
        "passed": 5,
        "total_tasks": 5,
        "verifier_pass_rate": 1.0,
        "within_budget_rate": 1.0,
    }}))
    run_context_governance_evaluation(context)
    run_project_memory_evaluation(project)
    run_repo_map_evaluation(repo)
    report_path = tmp_path / "report.md"
    text = write_runtime_report(
        report_path,
        context,
        project,
        repo,
        harness_path=harness,
    )
    assert report_path.exists()
    assert "Runtime mechanisms, not model intelligence" in text
