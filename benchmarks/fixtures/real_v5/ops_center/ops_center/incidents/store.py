def mark_resolved(incidents, incident_id):
    incidents[incident_id]["status"] = "resolved"
