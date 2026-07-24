from ops_center.incidents.dependencies import open_blockers
from ops_center.incidents.store import mark_resolved


def resolve(incidents, audit_log, incident_id, resolver):
    incident = incidents[incident_id]
    if incident.get("status") == "resolved":
        return False
    if open_blockers(incidents, incident_id):
        raise ValueError("open blocker")
    mark_resolved(incidents, incident_id)
    audit_log.append(
        {
            "incident_id": incident_id,
            "resolver": resolver,
            "action": "resolved",
        }
    )
    return True
