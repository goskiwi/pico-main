def open_blockers(incidents, incident_id):
    incident = incidents[incident_id]
    return [
        blocker_id
        for blocker_id in incident.get("blockers", [])
        if incidents[blocker_id].get("status") != "resolved"
    ]
