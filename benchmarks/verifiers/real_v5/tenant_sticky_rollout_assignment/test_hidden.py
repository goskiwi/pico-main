from copy import deepcopy

import pytest

from ops_center.rollouts.api import evaluate_flag
from ops_center.rollouts.cohorts import cohort_bucket


def test_existing_assignments_are_tenant_scoped_and_sticky():
    flags = {"new-ui": {"percentage": 0, "salt": "v1"}}
    assignments = {
        ("tenant-a", "shared-user", "new-ui"): True,
        ("tenant-b", "shared-user", "new-ui"): False,
    }

    assert evaluate_flag(
        flags,
        assignments,
        "tenant-a",
        "shared-user",
        "new-ui",
    )
    assert not evaluate_flag(
        flags,
        assignments,
        "tenant-b",
        "shared-user",
        "new-ui",
    )
    assert len(assignments) == 2


def test_new_assignment_uses_stable_cohort_and_full_key():
    flags = {"beta": {"percentage": 37, "salt": "launch"}}
    assignments = {}
    expected = cohort_bucket("beta", "launch", "user-9") < 37

    actual = evaluate_flag(
        flags,
        assignments,
        "tenant-z",
        "user-9",
        "beta",
    )

    assert actual is expected
    assert assignments == {
        ("tenant-z", "user-9", "beta"): expected,
    }


def test_percentage_change_does_not_replace_existing_assignment():
    flags = {"beta": {"percentage": 100, "salt": "v1"}}
    assignments = {}
    assert evaluate_flag(
        flags,
        assignments,
        "tenant-a",
        "user-1",
        "beta",
    )
    flags["beta"]["percentage"] = 0

    assert evaluate_flag(
        flags,
        assignments,
        "tenant-a",
        "user-1",
        "beta",
    )


@pytest.mark.parametrize(
    ("tenant_id", "user_id", "flag_name", "error"),
    [
        ("", "user-1", "beta", KeyError),
        ("tenant-a", "", "beta", KeyError),
        ("tenant-a", "user-1", "missing", KeyError),
    ],
)
def test_missing_identifiers_or_flag_are_atomic(
    tenant_id,
    user_id,
    flag_name,
    error,
):
    flags = {"beta": {"percentage": 50, "salt": "v1"}}
    assignments = {("existing", "user", "beta"): True}
    before = deepcopy(assignments)

    with pytest.raises(error):
        evaluate_flag(
            flags,
            assignments,
            tenant_id,
            user_id,
            flag_name,
        )

    assert assignments == before


def test_invalid_percentage_is_atomic():
    flags = {"beta": {"percentage": 101, "salt": "v1"}}
    assignments = {}

    with pytest.raises(ValueError):
        evaluate_flag(
            flags,
            assignments,
            "tenant-a",
            "user-1",
            "beta",
        )

    assert assignments == {}
