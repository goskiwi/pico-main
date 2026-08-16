from pico.evaluation.metrics import (
    run_context_governance_ablation,
    run_project_memory_evaluation,
    run_repo_map_evaluation,
    run_runtime_governance_evaluation,
    run_working_memory_ablation,
    write_runtime_report,
)


def test_context_governance_ablation_is_budgeted(tmp_path):
    artifact = run_context_governance_ablation(tmp_path / "context.json", repetitions=1)
    assert artifact["summary"]["within_budget_rate"] == 1.0
    assert artifact["summary"]["current_request_preserved_rate"] == 1.0
    assert artifact["summary"]["mean_token_reduction"] > 0


def test_working_memory_ablation_rejects_stale_revision(tmp_path):
    artifact = run_working_memory_ablation(tmp_path / "working.json", repetitions=1)
    assert artifact["variants"]["memory_on"]["hit_rate"] == 1.0
    assert artifact["variants"]["stale_revision"]["hit_rate"] == 0.0
    assert artifact["variants"]["memory_on"]["mean_repeated_reads"] == 0


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
    working = tmp_path / "working.json"
    project = tmp_path / "project.json"
    repo = tmp_path / "repo.json"
    run_context_governance_ablation(context, repetitions=1)
    run_working_memory_ablation(working, repetitions=1)
    run_project_memory_evaluation(project)
    run_repo_map_evaluation(repo)
    report_path = tmp_path / "report.md"
    text = write_runtime_report(report_path, context, working, project, repo)
    assert report_path.exists()
    assert "Runtime mechanisms, not model intelligence" in text


def test_runtime_governance_evaluation_is_replayable(tmp_path):
    artifact = run_runtime_governance_evaluation(tmp_path / "runtime.json")
    assert all(artifact["summary"].values())
