from pico.evaluation.gates import evaluation_failures


def passing_results():
    return {
        "harness": {"summary": {"passed": 5, "total_tasks": 5}},
        "context": {
            "summary": {
                "within_budget_rate": 1.0,
                "current_request_preserved_rate": 1.0,
            }
        },
        "project_memory": {"summary": {"catalog_generated": True}},
        "repo_map": {"summary": {"query_hit": True}},
        "runtime_policy": {"summary": {"journal_valid": True}},
    }


def test_evaluation_failures_accepts_all_passing_contracts():
    assert evaluation_failures(passing_results()) == []


def test_evaluation_failures_reports_harness_and_mechanism_regressions():
    results = passing_results()
    results["harness"]["summary"]["passed"] = 4
    results["repo_map"]["summary"]["query_hit"] = False

    assert evaluation_failures(results) == [
        "native Harness passed 4/5",
        "repo_map failed checks: query_hit",
    ]
