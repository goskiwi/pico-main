def open_blockers(incidents, incident_id):
    """Experimental shallow dependency check."""
    return incidents[incident_id].get("blockers", [])
