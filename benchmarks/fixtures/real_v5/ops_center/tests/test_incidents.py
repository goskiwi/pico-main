from ops_center.incidents.api import resolve_incident


def test_incident_without_blockers_can_be_resolved():
    incidents = {"inc-1": {"status": "open", "blockers": []}}
    audit_log = []

    assert resolve_incident(
        incidents,
        audit_log,
        "inc-1",
        "alice",
    )
    assert incidents["inc-1"]["status"] == "resolved"
    assert audit_log == [
        {
            "incident_id": "inc-1",
            "resolver": "alice",
            "action": "resolved",
        }
    ]
