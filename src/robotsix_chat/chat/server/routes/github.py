"""GitHub repository endpoints.

``PATCH /chat/github/repos/{owner}/{repo}/settings`` — toggle
security-and-analysis features on repos reachable through the
configured GitHub App installation.

``GET /chat/github/repos/{owner}/{repo}/actions/jobs/{job_id}/logs`` —
fetch the plain-text log for a GitHub Actions job, following the
GitHub redirect server-side so the caller receives the log content
directly.
"""

from __future__ import annotations

import json
import logging

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from robotsix_chat.config.models import (
    GitHubActionsSettings,
    GitHubSecuritySettings,
)
from robotsix_chat.repo.direct.client import DirectRepoClient

logger = logging.getLogger(__name__)


async def _github_endpoint(
    request: Request,
    settings: GitHubSecuritySettings | GitHubActionsSettings,
    detail_prefix: str,
) -> tuple[str, str, dict[str, object]]:
    """Shared boilerplate for GitHub endpoints.

    Handles:
    1. Settings check (503)
    2. API key auth (403)
    3. Path-param extraction (owner, repo) (400)
    4. JSON body parse + dict validation (400)

    Returns ``(owner, repo, body)``.  Does **not** perform the
    installation scope check — callers must call
    ``_check_installation_scope`` after their own body validation.
    """
    direct_repo = request.app.state.direct_repo_settings

    # -- 503: unconfigured -------------------------------------------------
    if not settings.enabled or not direct_repo.enabled:
        raise HTTPException(status_code=503, detail=f"{detail_prefix} is not enabled")
    api_key = settings.deploy_api_key.get_secret_value()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail=f"{detail_prefix}.deploy_api_key is not configured",
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

    # -- body --------------------------------------------------------------
    try:
        body = await request.json()
    except json.JSONDecodeError, ValueError:
        raise HTTPException(status_code=400, detail="invalid JSON body") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")

    return owner, repo, body


async def _check_installation_scope(
    request: Request,
    repo_full_name: str,
) -> DirectRepoClient:
    """Check that *repo_full_name* is in the installation scope.

    Returns a ``DirectRepoClient`` ready for API calls.
    Raises ``HTTPException`` on failure (404/502).
    """
    direct_repo = request.app.state.direct_repo_settings
    client = DirectRepoClient(direct_repo)
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

    return client


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
    owner, repo, body = await _github_endpoint(request, settings, "github_security")
    repo_full_name = f"{owner}/{repo}"

    # -- body validation (before scope check) ------------------------------
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

    # -- scope check -------------------------------------------------------
    client = await _check_installation_scope(request, repo_full_name)

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


async def github_actions_secret_endpoint(request: Request) -> JSONResponse:
    """Handle ``PUT /chat/github/repos/{owner}/{repo}/actions/secrets/{secret_name}``.

    Create or update a repository Actions secret.  Requires an
    ``X-API-Key`` header matching the configured ``deploy_api_key``.

    Path parameters:
        owner: GitHub organisation or user name.
        repo: Repository name (not owner/repo).
        secret_name: Actions secret name (e.g. ``OVH_SFTP_HOST``).

    JSON body:
        secret_value: string — the plaintext value to encrypt and store.

    Returns:
        200 — secret set successfully.
        400 — invalid body or missing path params.
        403 — invalid or missing X-API-Key.
        404 — repository not in the GitHub App installation scope.
        503 — github_actions not configured (disabled or missing key).

    """
    settings = request.app.state.github_actions_settings
    owner, repo, body = await _github_endpoint(request, settings, "github_actions")
    repo_full_name = f"{owner}/{repo}"

    # -- extra path param --------------------------------------------------
    secret_name = request.path_params.get("secret_name", "").strip()
    if not secret_name:
        raise HTTPException(
            status_code=400,
            detail="owner, repo, and secret_name path parameters are required",
        )

    # -- validate body (before scope check) --------------------------------
    secret_value = body.get("secret_value")
    if not secret_value or not isinstance(secret_value, str):
        raise HTTPException(
            status_code=400,
            detail="'secret_value' (string) is required",
        )

    # -- scope check -------------------------------------------------------
    client = await _check_installation_scope(request, repo_full_name)

    result = await client.set_actions_secret(
        repo_full_name,
        secret_name=secret_name,
        secret_value=secret_value,
    )

    if result.startswith("Error"):
        raise HTTPException(status_code=502, detail=result)

    return JSONResponse(
        {
            "status": "ok",
            "repo": repo_full_name,
            "secret_name": secret_name,
            "message": result,
        }
    )


async def github_actions_workflow_endpoint(request: Request) -> JSONResponse:
    """Handle ``POST .../actions/workflows/{workflow_id}/dispatches``.

    Trigger a workflow_dispatch event.  Requires an ``X-API-Key`` header
    matching the configured ``deploy_api_key``.

    Path parameters:
        owner: GitHub organisation or user name.
        repo: Repository name (not owner/repo).
        workflow_id: Workflow file name or numeric workflow ID.

    JSON body:
        ref: string — branch or tag to run on (required).
        inputs: object — optional key/value workflow inputs.

    Returns:
        200 — workflow dispatched successfully.
        400 — invalid body or missing path params.
        403 — invalid or missing X-API-Key.
        404 — repository not in the GitHub App installation scope.
        503 — github_actions not configured (disabled or missing key).

    """
    settings = request.app.state.github_actions_settings
    owner, repo, body = await _github_endpoint(request, settings, "github_actions")
    repo_full_name = f"{owner}/{repo}"

    # -- extra path param --------------------------------------------------
    workflow_id = request.path_params.get("workflow_id", "").strip()
    if not workflow_id:
        raise HTTPException(
            status_code=400,
            detail="owner, repo, and workflow_id path parameters are required",
        )

    # -- validate body (before scope check) --------------------------------
    ref = body.get("ref")
    if not ref or not isinstance(ref, str):
        raise HTTPException(
            status_code=400,
            detail="'ref' (string) is required",
        )

    inputs: dict[str, str] | None = None
    raw_inputs = body.get("inputs")
    if raw_inputs is not None:
        if not isinstance(raw_inputs, dict):
            raise HTTPException(
                status_code=400,
                detail="'inputs' must be a JSON object",
            )
        inputs = {str(k): str(v) for k, v in raw_inputs.items()}

    # -- scope check -------------------------------------------------------
    client = await _check_installation_scope(request, repo_full_name)

    result = await client.dispatch_workflow(
        repo_full_name,
        workflow_id=workflow_id,
        ref=ref,
        inputs=inputs,
    )

    if result.startswith("Error"):
        raise HTTPException(status_code=502, detail=result)

    return JSONResponse(
        {
            "status": "ok",
            "repo": repo_full_name,
            "workflow_id": workflow_id,
            "message": result,
        }
    )
async def github_job_log_endpoint(request: Request) -> PlainTextResponse:
    """Handle ``GET /chat/github/repos/{owner}/{repo}/actions/jobs/{job_id}/logs``.

    Fetches the plain-text log for a GitHub Actions job.  The GitHub API
    returns a 302 redirect to a signed URL; this endpoint follows it
    server-side and returns the log text as a 200 response.
        job_id: GitHub Actions job ID (integer).

    Requires an ``X-API-Key`` header matching the configured
    ``deploy_api_key``.

    Returns:
        200 — plain-text job log.
        400 — missing path params or invalid job_id.
        403 — invalid or missing X-API-Key.
        404 — repository not in the GitHub App installation scope, or
              job not found.
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
    job_id_raw = request.path_params.get("job_id", "").strip()
    if not owner or not repo or not job_id_raw:
        raise HTTPException(
            status_code=400,
            detail="owner, repo, and job_id path parameters are required",
        )
    try:
        job_id = int(job_id_raw)
    except ValueError, TypeError:
        raise HTTPException(
            status_code=400, detail=f"job_id must be an integer, got {job_id_raw!r}"
        ) from None
    repo_full_name = f"{owner}/{repo}"

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

    try:
        log_text = await client.get_job_log(repo_full_name, job_id)
    except RuntimeError as exc:
        msg = str(exc)
        if "404" in msg:
            raise HTTPException(
                status_code=404,
                detail=f"job {job_id} not found in {repo_full_name}: {msg}",
            ) from None
        raise HTTPException(status_code=502, detail=msg) from None
    except Exception as exc:
        logger.exception("Failed to fetch job log")
        raise HTTPException(
            status_code=502, detail=f"GitHub API error: {exc}"
        ) from None

    return PlainTextResponse(log_text, status_code=200)
