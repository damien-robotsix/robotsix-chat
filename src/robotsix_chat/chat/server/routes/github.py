"""GitHub repository settings endpoint.

``PATCH /chat/github/repos/{owner}/{repo}/settings`` — toggle
security-and-analysis features on repos reachable through the
configured GitHub App installation.
"""

from __future__ import annotations

import json
import logging

from starlette.exceptions import HTTPException
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
        raise HTTPException(status_code=503, detail="github_security is not enabled")
    api_key = settings.deploy_api_key.get_secret_value()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="github_security.deploy_api_key is not configured",
        )

    # -- 403: auth ---------------------------------------------------------
    presented = request.headers.get("X-API-Key", "")
    if not presented or presented != api_key:
        raise HTTPException(status_code=403, detail="invalid or missing X-API-Key")

    # -- path params -------------------------------------------------------
    owner = request.path_params.get("owner", "").strip()
    repo = request.path_params.get("repo", "").strip()
    if not owner or not repo:
        raise HTTPException(
            status_code=400,
            detail="owner and repo path parameters are required",
        )
    repo_full_name = f"{owner}/{repo}"

    # -- body --------------------------------------------------------------
    try:
        body = await request.json()
    except json.JSONDecodeError, ValueError:
        raise HTTPException(status_code=400, detail="invalid JSON body") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")

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
            raise HTTPException(
                status_code=400,
                detail=f"{key} must be 'enabled' or 'disabled', got {val!r}",
            )
        else:
            kwargs[key] = None

    if not any(v is not None for v in kwargs.values()):
        raise HTTPException(
            status_code=400,
            detail="at least one security feature must be specified",
        )

    # -- call --------------------------------------------------------------
    client = DirectRepoClient(direct_repo)

    # Check installation scope (404 if repo not accessible).
    try:
        allowed = await client.list_installation_repos()
    except Exception as exc:
        logger.exception("Failed to list installation repos")
        raise HTTPException(
            status_code=502, detail=f"GitHub API error: {exc}"
        ) from None

    if repo_full_name not in allowed:
        raise HTTPException(
            status_code=404,
            detail=(
                f"repo '{repo_full_name}' is not in the GitHub App installation scope"
            ),
        )

    result = await client.set_security_and_analysis(
        repo_full_name,
        dependency_graph=kwargs["dependency_graph"],
        advanced_security=kwargs["advanced_security"],
        secret_scanning=kwargs["secret_scanning"],
        secret_scanning_push_protection=kwargs["secret_scanning_push_protection"],
    )

    if result.startswith("Error"):
        raise HTTPException(status_code=502, detail=result)

    return JSONResponse(
        {
            "status": "ok",
            "repo": repo_full_name,
            "message": result,
        }
    )
