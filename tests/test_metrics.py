import json

from pico.evaluation.metrics import (
    run_context_governance_ablation,
    run_project_memory_evaluation,
    run_repo_map_evaluation,
    run_runtime_policy_evaluation,
    write_runtime_report,
)


def test_context_governance_ablation_is_budgeted(tmp_path):
    artifact = run_context_governance_ablation(tmp_path / "context.json", repetitions=1)
    assert artifact["summary"]["within_budget_rate"] == 1.0
    assert artifact["summary"]["current_request_preserved_rate"] == 1.0
    assert artifact["summary"]["mean_token_reduction"] > 0
    assert artifact["runtime_snapshot_id"].startswith("sha256:")


def test_project_memory_and_repo_map_evaluations(tmp_path):
    project = run_project_memory_evaluation(tmp_path / "project.json")
    repo = run_repo_map_evaluation(tmp_path / "repo.json")
    assert all(project["summary"].values())
    assert repo["summary"] == {
        "query_hit": True,
        "within_budget": True,
        "index_revision_bound": True,
    }


def test_runtime_report_uses_replayable_artifacts(tmp_path):
    context = tmp_path / "context.json"
    project = tmp_path / "project.json"
    repo = tmp_path / "repo.json"
    policy = tmp_path / "policy.json"
    harness = tmp_path / "harness.json"
    harness.write_text(json.dumps({"summary": {
        "passed": 5,
        "total_tasks": 5,
        "verifier_pass_rate": 1.0,
        "within_budget_rate": 1.0,
    }}))
    run_context_governance_ablation(context, repetitions=1)
    run_project_memory_evaluation(project)
    run_repo_map_evaluation(repo)
    run_runtime_policy_evaluation(policy)
    report_path = tmp_path / "report.md"
    text = write_runtime_report(
        report_path,
        context,
        project,
        repo,
        runtime_policy_path=policy,
        harness_path=harness,
    )
    assert report_path.exists()
    assert "Runtime mechanisms, not model intelligence" in text


def test_runtime_policy_evaluation_is_replayable(tmp_path):
    artifact = run_runtime_policy_evaluation(tmp_path / "runtime.json")
    assert all(artifact["summary"].values())
