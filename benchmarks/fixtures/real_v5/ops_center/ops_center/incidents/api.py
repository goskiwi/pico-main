from ops_center.incidents.service import resolve


def resolve_incident(incidents, audit_log, incident_id, resolver):
    """Resolve an incident after validating its dependency graph."""
    return resolve(incidents, audit_log, incident_id, resolver)
