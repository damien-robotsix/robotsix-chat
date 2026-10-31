"""OpenAPI schema and Swagger UI endpoints for the chat server.

:data:`_API_ROUTE_SPECS` is the single source of truth for the versioned API
routes; ``create_app`` mounts them and :func:`_build_openapi_schema` documents
them.
"""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from robotsix_chat import PROJECT_TITLE

from .routes import (
    auth_callback_endpoint,
    auth_login_endpoint,
    cancel_queued_endpoint,
    chat_endpoint,
    config_deploy_get_endpoint,
    config_get_endpoint,
    config_rollback_endpoint,
    config_save_endpoint,
    config_version_diff_endpoint,
    config_version_get_endpoint,
    config_versions_endpoint,
    diagnostics_create_endpoint,
    diagnostics_list_endpoint,
    disk_usage_endpoint,
    draft_get_endpoint,
    draft_save_endpoint,
    events_endpoint,
    github_actions_secret_endpoint,
    github_actions_workflow_endpoint,
    github_job_log_endpoint,
    github_repo_create_endpoint,
    github_settings_endpoint,
    health_endpoint,
    history_endpoint,
    metrics_endpoint,
    mill_events_endpoint,
    mobile_token_endpoint,
    models_list_endpoint,
    periodic_definitions_list_endpoint,
    periodic_definitions_run_endpoint,
    prune_endpoint,
    session_model_set_endpoint,
    sessions_close_endpoint,
    sessions_create_endpoint,
    sessions_delete_endpoint,
    sessions_list_endpoint,
    subsessions_close_endpoint,
    subsessions_get_endpoint,
    subsessions_list_endpoint,
    subsessions_message_endpoint,
    subsessions_transcript_endpoint,
)

# ---------------------------------------------------------------------------
# OpenAPI contract table & schema
# ---------------------------------------------------------------------------

# One row per HTTP endpoint: ``(path, endpoint, methods, summary, versioned)``.
# Ops probes (``/health``, ``/metrics``) are root-only; every other endpoint is
# registered both at its historical root path (so existing un-versioned callers
# keep working during the transition) and under the stable ``/api/v1`` prefix.
# The OpenAPI document is derived from this single source of truth so the served
# schema never drifts from the registered routes.
_API_ROUTE_SPECS: tuple[tuple[str, Any, tuple[str, ...], str, bool], ...] = (
    (
        "/health",
        health_endpoint,
        ("GET",),
        "Liveness probe — returns the service status.",
        False,
    ),
    (
        "/metrics",
        metrics_endpoint,
        ("GET",),
        "Prometheus metrics scrape endpoint.",
        False,
    ),
    (
        "/auth/login",
        auth_login_endpoint,
        ("GET",),
        "Mint a subject token and redirect to the mobile SSO app.",
        True,
    ),
    (
        "/auth/callback",
        auth_callback_endpoint,
        ("GET",),
        "Mobile SSO callback — exchange an auth code for a session.",
        True,
    ),
    (
        "/chat/auth/mobile-token",
        mobile_token_endpoint,
        ("POST",),
        "Exchange a subject token for a short-lived bearer access token.",
        True,
    ),
    (
        "/admin/disk",
        disk_usage_endpoint,
        ("GET",),
        "Report on-disk usage of the service volumes.",
        True,
    ),
    (
        "/admin/prune",
        prune_endpoint,
        ("POST",),
        "Prune stale or excess stored data.",
        True,
    ),
    (
        "/mill-events",
        mill_events_endpoint,
        ("POST",),
        "Ingest a mill (workflow) event.",
        True,
    ),
    (
        "/chat",
        chat_endpoint,
        ("POST",),
        "Submit a chat message; the agent reply is streamed back over SSE.",
        True,
    ),
    (
        "/chat/queue/cancel",
        cancel_queued_endpoint,
        ("POST",),
        "Cancel a queued (not yet running) chat turn.",
        True,
    ),
    (
        "/events",
        events_endpoint,
        ("GET",),
        "Server-Sent Events stream of chat and agent events.",
        True,
    ),
    (
        "/history",
        history_endpoint,
        ("GET",),
        "List the conversation history.",
        True,
    ),
    (
        "/models",
        models_list_endpoint,
        ("GET",),
        "List the available chat model levels.",
        True,
    ),
    (
        "/sessions",
        sessions_list_endpoint,
        ("GET",),
        "List chat sessions.",
        True,
    ),
    (
        "/sessions",
        sessions_create_endpoint,
        ("POST",),
        "Create a new chat session.",
        True,
    ),
    (
        "/sessions/{session_id}/model",
        session_model_set_endpoint,
        ("POST",),
        "Set the model level used by a session.",
        True,
    ),
    (
        "/sessions/{session_id}",
        sessions_delete_endpoint,
        ("DELETE",),
        "Delete a chat session.",
        True,
    ),
    (
        "/sessions/{session_id}/close",
        sessions_close_endpoint,
        ("POST",),
        "Close a chat session.",
        True,
    ),
    (
        "/periodic/definitions",
        periodic_definitions_list_endpoint,
        ("GET",),
        "List periodic (scheduled) session definitions.",
        True,
    ),
    (
        "/periodic/definitions/{name}/run",
        periodic_definitions_run_endpoint,
        ("POST",),
        "Run a periodic session definition on demand.",
        True,
    ),
    (
        "/sessions/{session_id}/draft",
        draft_get_endpoint,
        ("GET",),
        "Fetch the current draft for a session.",
        True,
    ),
    (
        "/sessions/{session_id}/draft",
        draft_save_endpoint,
        ("PUT",),
        "Save the current draft for a session.",
        True,
    ),
    (
        "/subsessions",
        subsessions_list_endpoint,
        ("GET",),
        "List subsessions.",
        True,
    ),
    (
        "/subsessions/{sub_id}",
        subsessions_get_endpoint,
        ("GET",),
        "Fetch a subsession.",
        True,
    ),
    (
        "/subsessions/{sub_id}/transcript",
        subsessions_transcript_endpoint,
        ("GET",),
        "Fetch a subsession transcript.",
        True,
    ),
    (
        "/subsessions/{sub_id}/message",
        subsessions_message_endpoint,
        ("POST",),
        "Send a message into a subsession.",
        True,
    ),
    (
        "/subsessions/{sub_id}/close",
        subsessions_close_endpoint,
        ("POST",),
        "Close a subsession.",
        True,
    ),
    (
        "/chat/github/repos",
        github_repo_create_endpoint,
        ("POST",),
        "Create a GitHub repository.",
        True,
    ),
    (
        "/chat/github/repos/{owner}/{repo}/settings",
        github_settings_endpoint,
        ("PATCH",),
        "Update GitHub repository settings.",
        True,
    ),
    (
        "/chat/github/repos/{owner}/{repo}/actions/secrets/{secret_name}",
        github_actions_secret_endpoint,
        ("PUT",),
        "Set a GitHub Actions repository secret.",
        True,
    ),
    (
        "/chat/github/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches",
        github_actions_workflow_endpoint,
        ("POST",),
        "Dispatch a GitHub Actions workflow.",
        True,
    ),
    (
        "/chat/github/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
        github_job_log_endpoint,
        ("GET",),
        "Fetch a GitHub Actions job log.",
        True,
    ),
    (
        "/config",
        config_get_endpoint,
        ("GET",),
        "Read the effective configuration.",
        True,
    ),
    (
        "/config/deploy",
        config_deploy_get_endpoint,
        ("GET",),
        "Read the deploy-plane configuration.",
        True,
    ),
    (
        "/config",
        config_save_endpoint,
        ("PUT",),
        "Save/update the configuration.",
        True,
    ),
    (
        "/config/versions",
        config_versions_endpoint,
        ("GET",),
        "List the configuration versions.",
        True,
    ),
    (
        "/config/versions/{version:int}",
        config_version_get_endpoint,
        ("GET",),
        "Read a specific configuration version.",
        True,
    ),
    (
        "/config/versions/{version:int}/diff",
        config_version_diff_endpoint,
        ("GET",),
        "Diff two configuration versions.",
        True,
    ),
    (
        "/config/rollback",
        config_rollback_endpoint,
        ("POST",),
        "Roll back to a previous configuration version.",
        True,
    ),
    (
        "/diagnostics/events",
        diagnostics_create_endpoint,
        ("POST",),
        "Record a diagnostic event.",
        True,
    ),
    (
        "/diagnostics/events",
        diagnostics_list_endpoint,
        ("GET",),
        "List the recorded diagnostic events.",
        True,
    ),
)


_OPENAPI_RESPONSES: dict[str, dict[str, str]] = {
    "200": {"description": "Success."},
    "400": {"description": "Bad request."},
    "404": {"description": "Not found."},
    "405": {"description": "Method not allowed."},
    "500": {"description": "Internal server error."},
}


def _openapi_schema_path(path: str) -> str:
    """Normalize a Starlette route path for the OpenAPI document.

    Path converters (``{version:int}``) are stripped to plain
    ``{version}`` placeholders.
    """
    return re.sub(r"\{(\w+):\w+\}", r"{\1}", path)


def _openapi_path_params(path: str) -> list[dict[str, Any]]:
    """Derive ``in: path`` parameters from a route's ``{placeholder}`` names."""
    return [
        {
            "name": name,
            "in": "path",
            "required": True,
            "schema": {"type": "string"},
        }
        for name in re.findall(r"\{(\w+)(?::\w+)?\}", path)
    ]


def _openapi_operation(
    path: str, methods: tuple[str, ...], summary: str
) -> dict[str, Any]:
    """Build one OpenAPI operation object for a path/method pair."""
    operation: dict[str, Any] = {
        "summary": summary,
        "responses": dict(_OPENAPI_RESPONSES),
    }
    params = _openapi_path_params(path)
    if params:
        operation["parameters"] = params
    if any(method in ("POST", "PUT", "PATCH") for method in methods):
        operation["requestBody"] = {
            "content": {"application/json": {"schema": {"type": "object"}}}
        }
    return operation


def _build_openapi_schema() -> dict[str, Any]:
    """Assemble the OpenAPI 3.0.2 document from :data:`_API_ROUTE_SPECS`.

    Versioned endpoints appear both at their historical root path and under
    the stable ``/api/v1`` prefix, so the schema covers every served path.
    """
    paths: dict[str, Any] = {}
    for path, _endpoint, methods, summary, versioned in _API_ROUTE_SPECS:
        served_paths = (path,) if not versioned else (path, f"/api/v1{path}")
        for served_path in served_paths:
            schema_path = _openapi_schema_path(served_path)
            paths.setdefault(schema_path, {})
            for method in methods:
                paths[schema_path][method.lower()] = _openapi_operation(
                    schema_path, (method,), summary
                )
    return {
        "openapi": "3.0.2",
        "info": {
            "title": PROJECT_TITLE,
            "version": "1.0.0",
        },
        "paths": paths,
    }


_OPENAPI_SCHEMA = _build_openapi_schema()


# The SwaggerUI bootstrap is the page's only inline script.  Its SHA-256 is
# allow-listed in the app's Content-Security-Policy ``script-src`` (see
# ``app.py``) so /docs keeps working without ``'unsafe-inline'``.
_SWAGGER_INIT_SCRIPT = """
  window.onload = () => {
    window.ui = SwaggerUIBundle({ url: "/openapi.json", dom_id: "#swagger-ui" });
  };
"""
SWAGGER_INIT_SCRIPT_CSP_SOURCE = (
    "'sha256-"
    + base64.b64encode(hashlib.sha256(_SWAGGER_INIT_SCRIPT.encode()).digest()).decode()
    + "'"
)

_SWAGGER_UI_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{PROJECT_TITLE} — API docs</title>
<link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css" />
</head>
<body>
<div id="swagger-ui"></div>
<script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>{_SWAGGER_INIT_SCRIPT}</script>
</body>
</html>
"""


async def openapi_json_endpoint(_request: Request) -> JSONResponse:
    """Serve the OpenAPI 3.0.2 document describing the chat HTTP API."""
    return JSONResponse(_OPENAPI_SCHEMA)


async def docs_endpoint(_request: Request) -> HTMLResponse:
    """Serve a SwaggerUI page that renders ``/openapi.json``."""
    return HTMLResponse(_SWAGGER_UI_HTML)
