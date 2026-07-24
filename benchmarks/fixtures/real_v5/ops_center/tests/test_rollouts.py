from ops_center.rollouts.api import evaluate_flag


def test_assignment_is_sticky_for_one_tenant():
    flags = {"new-ui": {"percentage": 50, "salt": "v1"}}
    assignments = {}

    first = evaluate_flag(
        flags,
        assignments,
        "tenant-a",
        "user-1",
        "new-ui",
    )
    flags["new-ui"]["percentage"] = 0
    second = evaluate_flag(
        flags,
        assignments,
        "tenant-a",
        "user-1",
        "new-ui",
    )

    assert second is first
