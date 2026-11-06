"""Allowed feedback target-repo resolution and finding classifiers."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from robotsix_chat.common.http import safe_http_request

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Allowed-repo resolution (dynamic — no static config)
# ---------------------------------------------------------------------------

# In-memory cache keyed by "repos": (fetched_at_monotonic, list_of_repo_ids).
_repo_cache: dict[str, tuple[float, list[str]]] = {}
_REPO_CACHE_TTL: float = 60.0  # seconds — short enough to pick up access changes

#: Used when no ``central_deploy.url`` is configured. The ``central-deploy``
#: hostname only resolves on the deploy stack's *internal* compose network;
#: a component attached solely to ``central-deploy-proxy`` (which is how chat
#: runs) cannot resolve it and gets "Name or service not known". Keeping it as
#: the fallback preserves behaviour for deployments where it does resolve,
#: but any real deployment should set ``central_deploy.url``.
_DEFAULT_DEPLOY_BASE_URL = "http://central-deploy:8100"


async def _resolve_allowed_repos(
    deploy_api_key: str, deploy_base_url: str = ""
) -> list[str]:
    """Resolve the set of allowed feedback target repos dynamically.

    Queries the deploy server's chat-component roster and the mill board's
    repo registry, then intersects the two on component/repo id.  The result
    is cached briefly (``_REPO_CACHE_TTL``) to avoid hammering deploy on
    every feedback run.

    *deploy_base_url* should be ``central_deploy.url`` — the address this
    deployment already knows reaches the deploy server. Empty falls back to
    :data:`_DEFAULT_DEPLOY_BASE_URL`.

    Falls back to ``["robotsix-chat"]`` when deploy is unreachable and logs
    a warning.
    """
    now = time.monotonic()
    entry = _repo_cache.get("repos")
    if entry is not None and (now - entry[0]) < _REPO_CACHE_TTL:
        return entry[1]

    result = await _do_resolve_allowed_repos(deploy_api_key, deploy_base_url)
    _repo_cache["repos"] = (now, result)
    return result


async def _do_resolve_allowed_repos(
    deploy_api_key: str, deploy_base_url: str = ""
) -> list[str]:
    """Resolve allowed repos by querying deploy and mill (no caching)."""
    # 1. Fetch components from deploy.
    base = (deploy_base_url or _DEFAULT_DEPLOY_BASE_URL).rstrip("/")
    deploy_url = f"{base}/chat/components"
    deploy_headers: dict[str, str] = {}
    if deploy_api_key:
        deploy_headers["X-API-Key"] = deploy_api_key

    deploy_result = await safe_http_request(
        "GET", deploy_url, headers=deploy_headers, label="Deploy roster"
    )
    if deploy_result.error:
        logger.warning(
            "Deploy roster unreachable (%s) — falling back to [robotsix-chat] only",
            deploy_result.error,
        )
        return ["robotsix-chat"]

    try:
        deploy_entries: list[dict[str, Any]] = json.loads(deploy_result.text or "[]")
    except json.JSONDecodeError:
        logger.warning("Deploy roster response is not valid JSON — falling back")
        return ["robotsix-chat"]

    deploy_ids: set[str] = {
        e["id"] for e in deploy_entries if isinstance(e, dict) and "id" in e
    }
    if not deploy_ids:
        logger.warning("Deploy roster is empty — falling back to [robotsix-chat] only")
        return ["robotsix-chat"]

    # 2. Fetch repos from mill board.
    mill_url = "http://mill:8077/repos"
    mill_result = await safe_http_request("GET", mill_url, label="Mill repos")
    if mill_result.error:
        logger.warning(
            "Mill repos unreachable (%s) — falling back to [robotsix-chat] only",
            mill_result.error,
        )
        return ["robotsix-chat"]

    try:
        mill_repos: list[dict[str, Any]] = json.loads(mill_result.text or "[]")
    except json.JSONDecodeError:
        logger.warning("Mill repos response is not valid JSON — falling back")
        return ["robotsix-chat"]

    # Mill's GET /repos returns entries keyed ``repo_id`` (with ``board_id``
    # and ``forge_remote_url``) — NOT ``id``. Reading only ``id`` made this
    # set permanently empty, so the deploy∩mill intersection always fell back
    # to [robotsix-chat] and every cross-repo feedback ticket was silently
    # dropped (observed 2026-09-01: file-hub tickets skipped on every run).
    # Tolerate both keys in case the mill API ever renames it back.
    mill_ids: set[str] = {
        r.get("repo_id") or r["id"]
        for r in mill_repos
        if isinstance(r, dict) and ("repo_id" in r or "id" in r)
    }

    # 3. Intersect — only repos that are both in the deploy roster AND
    #    registered on the mill board are valid targets.
    allowed = sorted(deploy_ids & mill_ids)
    if not allowed:
        logger.warning(
            "No repos in deploy/mill intersection (deploy=%s, mill=%s) — "
            "falling back to [robotsix-chat] only",
            sorted(deploy_ids),
            sorted(mill_ids),
        )
        allowed = ["robotsix-chat"]

    return allowed


# ---------------------------------------------------------------------------
# Board admission-policy handling
# ---------------------------------------------------------------------------

#: Markers in a ``POST /tickets/ingest`` HTTP 400 body that identify the
#: board's *admission policy* rejection (mill #3093 permanently removed the
#: investigation-ticket capability: the board now admits deployment tickets
#: only and rejects ``source_tag=robotsix-chat-feedback`` with guidance to
#: run the investigation as a chat subsession agent instead).  Matched
#: case-insensitively against the ``detail`` field (or raw body) so a
#: policy rejection is recognised as an outcome, not a transient failure.
_ADMISSION_POLICY_MARKERS: tuple[str, ...] = (
    "is not admitted on this board",
    "ingest_blocked_source_tags",
    "chat subsession agent",
)


def _is_admission_policy_block(resp: httpx.Response) -> bool:
    """Return True when *resp* is the board's admission-policy 400 rejection.

    Only an HTTP 400 whose body advertises one of
    :data:`_ADMISSION_POLICY_MARKERS` qualifies — a generic 400 (malformed
    payload, validation error) is still treated as a real failure.
    """
    if resp.status_code != 400:
        return False
    detail = ""
    try:
        body = resp.json()
    except json.JSONDecodeError, ValueError:
        body = None
    if isinstance(body, dict):
        raw_detail = body.get("detail")
        if isinstance(raw_detail, str):
            detail = raw_detail
    haystack = (detail or resp.text or "").lower()
    return any(marker in haystack for marker in _ADMISSION_POLICY_MARKERS)


#: Repo id that owns the chat system's own configuration.  A finding about
#: chat's periodic presets, prompts, or session behaviour belongs to this
#: repo (or to a config change) — it must never be routed to a component
#: repo.  Routing such a finding to a component produced mis-targeted PRs on
#: ``hexarchy`` (#304/#311/#312) for a finding about chat's ``board-gates-drain``
#: periodic preset, which lives only in chat's config volume under
#: ``periodic.sessions`` and is not in any repo tree.
_CHAT_SELF_REPO: str = "robotsix-chat"

#: Lower-cased subject markers that identify a finding as concerning the chat
#: system's own periodic presets / prompts / session behaviour.  Such findings
#: are owned by the chat repo regardless of what the analysis LLM guessed for
#: ``target_repo``.  Kept deliberately narrow — a broad match (e.g. any
#: ``config`` kind) would mis-route genuine component findings.
_CHAT_CONFIG_SUBJECT_MARKERS: tuple[str, ...] = (
    "periodic",
    "preset",
    "session behaviour",
    "session behavior",
    "system prompt",
    "drain-the-mill",
)


def _subject_is_chat_config(ticket: dict[str, Any]) -> bool:
    """Return ``True`` when *ticket* concerns chat's own periodic/session config.

    Matches the finding's title and description against
    :data:`_CHAT_CONFIG_SUBJECT_MARKERS`.  These findings belong to the chat
    repo (or a config change) — they must never be routed to a component repo.
    """
    subject = (
        (f"{ticket.get('title', '')}\n{ticket.get('description', '')}").strip().lower()
    )
    return any(marker in subject for marker in _CHAT_CONFIG_SUBJECT_MARKERS)
