"""Fail-closed checks for deterministic evaluation artifacts."""


def evaluation_failures(results):
    failures = []
    harness = results["harness"]["summary"]
    if harness["passed"] != harness["total_tasks"]:
        failures.append(
            f"native Harness passed {harness['passed']}/{harness['total_tasks']}"
        )

    context = results["context"]["summary"]
    if context["within_budget_rate"] != 1.0 or context["current_request_preserved_rate"] != 1.0:
        failures.append("context governance invariant failed")

    for name in ("project_memory", "repo_map", "runtime_policy"):
        failed_checks = [
            key for key, passed in results[name]["summary"].items() if passed is not True
        ]
        if failed_checks:
            failures.append(f"{name} failed checks: {', '.join(failed_checks)}")
    return failures
