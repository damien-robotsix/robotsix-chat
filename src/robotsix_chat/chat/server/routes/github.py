"""GitHub repository settings endpoint.

``PATCH /chat/github/repos/{owner}/{repo}/settings`` — toggle
security-and-analysis features on repos reachable through the
configured GitHub App installation.
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from robotsix_chat.repo.direct.client import DirectRepoClient

logger = logging.getLogger(__name__)


async def github_settings_endpoint(request: Request) -> JSONResponse:
    """Handle ``PATCH /chat/github/repos/{owner}/{repo}/settings``.

    Toggle repository security-and-analysis features.  Requires an
    ``X-API-Key`` header matching the configured ``deploy_api_key``.

    Path parameters:
        owner: GitHub organisation or user name.
        repo: Repository name (not owner/repo).

    JSON body (all fields optional — omitted fields are left unchanged):
        dependency_graph: ``"enabled"`` or ``"disabled"``.
        advanced_security: ``"enabled"`` or ``"disabled"``.
        secret_scanning: ``"enabled"`` or ``"disabled"``.
        secret_scanning_push_protection: ``"enabled"`` or ``"disabled"``.

    Returns:
        200 — settings applied successfully.
        400 — invalid body or missing path params.
        403 — invalid or missing X-API-Key.
        404 — repository not in the GitHub App installation scope.
        503 — github_security not configured (disabled or missing key).

    """
    settings = request.app.state.github_security_settings
    direct_repo = request.app.state.direct_repo_settings

    # -- 503: unconfigured -------------------------------------------------
    if not settings.enabled or not direct_repo.enabled:
        return JSONResponse(
            {"error": "github_security is not enabled"},
            status_code=503,
        )
    api_key = settings.deploy_api_key.get_secret_value()
    if not api_key:
        return JSONResponse(
            {"error": "github_security.deploy_api_key is not configured"},
            status_code=503,
        )

    # -- 403: auth ---------------------------------------------------------
    presented = request.headers.get("X-API-Key", "")
    if not presented or presented != api_key:
        return JSONResponse(
            {"error": "invalid or missing X-API-Key"},
            status_code=403,
        )

    # -- path params -------------------------------------------------------
    owner = request.path_params.get("owner", "").strip()
    repo = request.path_params.get("repo", "").strip()
    if not owner or not repo:
        return JSONResponse(
            {"error": "owner and repo path parameters are required"},
            status_code=400,
        )
    repo_full_name = f"{owner}/{repo}"

    # -- body --------------------------------------------------------------
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected a JSON object"}, status_code=400)

    valid = frozenset({"enabled", "disabled"})
    feature_keys = (
        "dependency_graph",
        "advanced_security",
        "secret_scanning",
        "secret_scanning_push_protection",
    )
    kwargs: dict[str, str | None] = {}
    for key in feature_keys:
        val = body.get(key)
        if val is None:
            kwargs[key] = None
        elif isinstance(val, str) and val in valid:
            kwargs[key] = val
        elif key in body:  # present but invalid
            return JSONResponse(
                {"error": (f"{key} must be 'enabled' or 'disabled', got {val!r}")},
                status_code=400,
            )
        else:
            kwargs[key] = None

    if not any(v is not None for v in kwargs.values()):
        return JSONResponse(
            {"error": "at least one security feature must be specified"},
            status_code=400,
        )

    # -- call --------------------------------------------------------------
    client = DirectRepoClient(direct_repo)

    # Check installation scope (404 if repo not accessible).
    try:
        allowed = await client.list_installation_repos()
    except Exception as exc:
        logger.exception("Failed to list installation repos")
        return JSONResponse(
            {"error": f"GitHub API error: {exc}"},
            status_code=502,
        )

    if repo_full_name not in allowed:
        return JSONResponse(
            {
                "error": (
                    f"repo '{repo_full_name}' is not in the GitHub App "
                    f"installation scope"
                ),
                "allowed_repos": sorted(allowed),
            },
            status_code=404,
        )

    result = await client.set_security_and_analysis(
        repo_full_name,
        dependency_graph=kwargs["dependency_graph"],
        advanced_security=kwargs["advanced_security"],
        secret_scanning=kwargs["secret_scanning"],
        secret_scanning_push_protection=kwargs["secret_scanning_push_protection"],
    )

    if result.startswith("Error"):
        return JSONResponse({"error": result}, status_code=502)

    return JSONResponse(
        {
            "status": "ok",
            "repo": repo_full_name,
            "message": result,
        }
    )
