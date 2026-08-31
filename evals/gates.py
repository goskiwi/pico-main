"""Fail-closed checks for repository-local evaluation artifacts."""


def evaluation_failures(results):
    failures = []
    harness = results["harness"]["summary"]
    if harness["passed"] != harness["total_tasks"]:
        failures.append(
            f"native Harness passed {harness['passed']}/{harness['total_tasks']}"
        )

    context = results["context"]["summary"]
    required_context_rates = (
        "within_budget_rate",
        "task_request_preserved_rate",
        "compaction_commit_rate",
        "tool_transaction_integrity_rate",
        "original_event_preservation_rate",
        "task_contract_preservation_rate",
    )
    if any(context[name] != 1.0 for name in required_context_rates):
        failures.append("context governance invariant failed")
    if context["mean_token_reduction"] <= 0:
        failures.append("context governance did not reduce tokens")

    for name in ("repo_map",):
        failed_checks = [
            key for key, passed in results[name]["summary"].items() if passed is not True
        ]
        if failed_checks:
            failures.append(f"{name} failed checks: {', '.join(failed_checks)}")
    return failures
