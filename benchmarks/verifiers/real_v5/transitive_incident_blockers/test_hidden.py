from copy import deepcopy

import pytest

from ops_center.incidents.api import resolve_incident


def test_transitive_open_blocker_rejects_without_mutation():
    incidents = {
        "target": {"status": "open", "blockers": ["middle"]},
        "middle": {"status": "resolved", "blockers": ["root"]},
        "root": {"status": "open", "blockers": []},
    }
    audit_log = [{"action": "created"}]
    before = deepcopy((incidents, audit_log))

    with pytest.raises(ValueError, match="open blocker"):
        resolve_incident(
            incidents,
            audit_log,
            "target",
            "alice",
        )

    assert (incidents, audit_log) == before


def test_all_resolved_transitive_blockers_allow_one_target_update():
    incidents = {
        "target": {"status": "open", "blockers": ["middle"]},
        "middle": {"status": "resolved", "blockers": ["root"]},
        "root": {"status": "resolved", "blockers": []},
        "other": {"status": "open", "blockers": []},
    }
    audit_log = []

    assert resolve_incident(
        incidents,
        audit_log,
        "target",
        "bob",
    )

    assert incidents["target"]["status"] == "resolved"
    assert incidents["other"]["status"] == "open"
    assert audit_log == [
        {
            "incident_id": "target",
            "resolver": "bob",
            "action": "resolved",
        }
    ]


def test_dependency_cycle_has_exact_error_and_is_atomic():
    incidents = {
        "target": {"status": "open", "blockers": ["middle"]},
        "middle": {"status": "resolved", "blockers": ["target"]},
    }
    audit_log = []
    before = deepcopy(incidents)

    with pytest.raises(
        ValueError,
        match="^incident dependency cycle$",
    ):
        resolve_incident(
            incidents,
            audit_log,
            "target",
            "alice",
        )

    assert incidents == before
    assert audit_log == []


def test_unknown_transitive_blocker_is_atomic():
    incidents = {
        "target": {"status": "open", "blockers": ["middle"]},
        "middle": {"status": "resolved", "blockers": ["missing"]},
    }
    audit_log = []
    before = deepcopy(incidents)

    with pytest.raises(KeyError):
        resolve_incident(
            incidents,
            audit_log,
            "target",
            "alice",
        )

    assert incidents == before
    assert audit_log == []


def test_already_resolved_target_is_a_complete_no_op():
    incidents = {
        "target": {
            "status": "resolved",
            "blockers": ["missing"],
        }
    }
    audit_log = [{"action": "existing"}]
    before = deepcopy((incidents, audit_log))

    assert not resolve_incident(
        incidents,
        audit_log,
        "target",
        "alice",
    )
    assert (incidents, audit_log) == before
