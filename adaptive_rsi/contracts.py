"""Stable API contracts for clients and automated checks."""

API_VERSION = 2

# Every write carries the token returned by /api/contract in this header. The
# handler reads the same constant, so the document cannot name a different one.
CSRF_HEADER = "X-RSI-CSRF"

# Paths the OpenAPI document declares. server.py dispatches far more routes on
# purpose: this is an explicit subset, not a generated catalog of the console.
OPENAPI_PATHS = ("/api/config", "/api/health", "/api/operations", "/api/state")

CAPABILITIES = {
    "operations": "background operation receipts, progress and safe cancellation",
    "desktop-update-drain": "serialized application replacement after runtime work drains",
    "task-batch": "preview, approval and restore of grouped tasks",
    "config-history": "read and undo configuration history with revision checks",
    "config-repair": "explicit repair of invalid project or global settings",
    "config-revisions": "optimistic concurrency for settings changes",
    "state-snapshot": "related state fields read from one database snapshot",
    "documented-state": "JSON schema for the state response",
    "partial-openapi": "OpenAPI document declares four stable endpoints; the descriptor lists the rest",
    "diagnostics-preview": "minimal allowlisted diagnostic preview with explicit-consent export",
    "shutdown-progress": "readable graceful shutdown phase and remaining work",
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


def _json(schema):
    return {"content": {"application/json": {"schema": schema}}}


def openapi(origin, cookie_name):
    """OpenAPI 3.1 document for the stable subset listed in OPENAPI_PATHS.

    The document states the transport rules the handler enforces for every
    request (Host allowlist) and every write (Origin plus CSRF token), so an
    integration written from it does not meet them as unexplained 403s.
    """
    error = {"$ref": "#/components/schemas/Error"}

    def ref(name):
        return {"$ref": "#/components/parameters/" + name}

    def denied(description="Host not in the allowlist, or Origin/CSRF token mismatch"):
        return {"401": {"description": "Session cookie missing or invalid (error_code=session_required)",
                        **_json(error)},
                "403": {"description": description, **_json(error)}}

    def invalid(description):
        return {"400": {"description": description, **_json(error)}, **denied()}

    def body(properties, required):
        return {"required": True, "content": {"application/json": {
            "schema": {"type": "object", "required": required, "properties": properties}}}}

    writes = [ref("Origin"), ref("Csrf")]
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Adaptive RSI local API",
            "version": str(API_VERSION),
            "description": (
                "Partial contract: only the four stable endpoints below are declared. The console "
                "dispatches many more /api routes, so GET /api/contract is the authoritative "
                "descriptor (api_version, capabilities, errors); do not treat this document as the "
                "full API surface. Every request needs a Host header from the server allowlist. "
                "Every write needs an Origin equal to the console origin and the X-RSI-CSRF token "
                "from GET /api/contract, otherwise the server answers 403 before dispatch. Write "
                "bodies must be application/json objects."),
        },
        "servers": [{"url": origin}],
        "paths": {
            "/api/health": {"get": {
                "summary": "Version, build and capability descriptor",
                "responses": {"200": {"description": "Health, version and descriptor fields",
                                      **_json({"type": "object"})}, **denied()}}},
            "/api/state": {"get": {
                "summary": "Project-scoped console state",
                "parameters": [ref("Project"), ref("Sections"), ref("Limit"), ref("Offset"),
                               ref("Filter"), ref("System")],
                "responses": {"200": {"description": "Project-scoped state",
                                      **_json({"type": "object", "description":
                                               "Full JSON schema: state_schema from GET /api/contract"})},
                              **invalid("Unknown section or invalid pagination")}}},
            "/api/config": {"post": {
                "summary": "Save settings with an optimistic revision check",
                "parameters": writes,
                "requestBody": body({
                    "values": {"type": "object"},
                    "project": {"type": ["string", "null"]},
                    "inherit": {"type": "array", "items": {"type": "string"}},
                    "revision": {"type": "integer", "description":
                                 "Expected revision; a stale value fails with config_revision_conflict"}},
                    ["values"]),
                "responses": {"200": {"description": "Saved settings and current revision",
                                      **_json({"type": "object"})},
                              **invalid("Invalid or conflicting configuration "
                                        "(config_revision_conflict, invalid_configuration, validation_error)")}}},
            "/api/operations": {"post": {
                "summary": "Start a background operation and return its receipt",
                "parameters": writes,
                "requestBody": body({"kind": {"type": "string"},
                                     "parameters": {"type": "object"}}, ["kind"]),
                "responses": {"200": {"description": "Background job receipt",
                                      **_json({"type": "object"})},
                              **invalid("Unknown kind or invalid parameters")}}},
        },
        "components": {
            "parameters": {
                "Origin": {"name": "Origin", "in": "header", "required": True,
                           "description": "Console origin (server origin or its local origin); any other value is rejected with 403",
                           "schema": {"type": "string"}},
                "Csrf": {"name": CSRF_HEADER, "in": "header", "required": True,
                         "description": "CSRF token from the csrf field of GET /api/contract",
                         "schema": {"type": "string"}},
                "Project": {"name": "project", "in": "query", "required": False,
                            "schema": {"type": "string"}},
                "Sections": {"name": "sections", "in": "query", "required": False,
                             "description": "Comma-separated subset of core, candidates, analytics; omit for the legacy full payload",
                             "schema": {"type": "string"}},
                "Limit": {"name": "limit", "in": "query", "required": False,
                          "schema": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}},
                "Offset": {"name": "offset", "in": "query", "required": False,
                           "schema": {"type": "integer", "minimum": 0, "default": 0}},
                "Filter": {"name": "filter", "in": "query", "required": False,
                           "schema": {"type": "string", "enum": ["all", "active", "attention", "history"]}},
                "System": {"name": "system", "in": "query", "required": False,
                           "description": "Include system tasks (true or false)",
                           "schema": {"type": "string", "enum": ["true", "false"]}},
            },
            "schemas": {"Error": {
                "type": "object", "required": ["error"],
                "properties": {
                    "error": {"type": "string"},
                    "error_code": {"type": "string", "description":
                                   "Stable code; the authoritative list is descriptor().errors from GET /api/contract"},
                    "reason_code": {"type": "string"},
                    "field_path": {"type": ["string", "null"]},
                }}},
            "securitySchemes": {"sessionCookie": {
                "type": "apiKey", "in": "cookie", "name": cookie_name,
                "description": "Session cookie printed by adaptive-rsi serve/open. Requests whose Host header is not in the server allowlist are rejected with 403 before this cookie is read."}},
        },
        "security": [{"sessionCookie": []}],
    }
