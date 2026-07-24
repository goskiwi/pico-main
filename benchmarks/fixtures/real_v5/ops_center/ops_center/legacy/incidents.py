def resolve_incident(incidents, audit_log, incident_id, resolver):
    """Deprecated unconditional incident close."""
    incidents[incident_id]["status"] = "resolved"
    return True
