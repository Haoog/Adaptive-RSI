"""Stable API contracts for clients and automated checks."""

API_VERSION = 2

CAPABILITIES = {
    "operations": "background operation receipts, progress and safe cancellation",
    "desktop-update-drain": "serialized application replacement after runtime work drains",
    "task-batch": "preview, approval and restore of grouped tasks",
    "config-history": "read and undo configuration history with revision checks",
    "config-repair": "explicit repair of invalid project or global settings",
    "config-revisions": "optimistic concurrency for settings changes",
    "state-snapshot": "related state fields read from one database snapshot",
    "documented-state": "JSON schema for the state response",
}

ERRORS = {
    "invalid_configuration": "A configuration field name, type or value is invalid",
    "invalid_request": "The request body or parameters are invalid",
    "config_revision_conflict": "Settings changed in another window",
    "session_required": "The local console session must be reopened",
    "internal_error": "The request could not be completed",
}


def descriptor():
    return {"api_version": API_VERSION, "capabilities": sorted(CAPABILITIES),
            "errors": ERRORS}
